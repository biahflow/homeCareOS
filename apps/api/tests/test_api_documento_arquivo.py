"""Testes de `GET /api/documentos/{id}/arquivo` — issue #51.

Contra Postgres **e** MinIO reais: o endpoint existe porque a URL assinada não
serve (ADR 0003), então o que precisa ser exercitado é justamente o caminho em
que a API lê do storage de verdade e transmite os bytes. Um fake de storage
provaria só que o handler chama o método certo.

O banco é compartilhado com outras trilhas: cada teste cria o que precisa sob
`COMPETENCIA_TESTE` (um valor que nenhum dado real usaria) e apaga o documento
e o objeto no teardown.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
from homecareos.db.models import Documento, DocumentoStatus, TipoDocumento
from homecareos.db.session import get_sessionmaker
from homecareos.intake.router import get_document_storage
from homecareos.main import app
from homecareos.storage import S3DocumentStorage, build_key
from tests.conftest import AUTH_HEADERS, TEST_API_KEY
from tests.fakes import FailingStorage, make_png

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
COMPETENCIA_TESTE = "2099-09"


def _postgres_responde(settings: Settings) -> str | None:
    try:
        engine = create_engine(
            settings.database_url, connect_args={"connect_timeout": SONDA_TIMEOUT}
        )
        try:
            with engine.connect() as conexao:
                conexao.execute(text("select 1"))
        finally:
            engine.dispose()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _boto3_client(settings: Settings) -> Any:
    """Cliente próprio do teste, para limpar o bucket sem passar pelo storage sob teste."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        aws_access_key_id=settings.s3_access_key or None,
        aws_secret_access_key=settings.s3_secret_key or None,
        region_name=settings.s3_region,
        config=Config(
            connect_timeout=SONDA_TIMEOUT,
            read_timeout=SONDA_TIMEOUT,
            retries={"max_attempts": 0},
        ),
    )


def _minio_responde(settings: Settings, client: Any) -> str | None:
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


@pytest.fixture(scope="module")
def settings() -> Settings:
    resolved = get_settings()
    motivo = _postgres_responde(resolved)
    if motivo is not None:
        pytest.skip(f"Postgres indisponível em {resolved.database_url}: {motivo}")
    motivo = _minio_responde(resolved, _boto3_client(resolved))
    if motivo is not None:
        pytest.skip(f"MinIO/bucket indisponível em {resolved.s3_endpoint_url}: {motivo}")
    return resolved


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"api_keys": TEST_API_KEY}
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def conteudo() -> bytes:
    """Os bytes "enviados": um PNG de verdade, como o que o intake grava."""
    return make_png()


@pytest.fixture
def documento(
    settings: Settings, sessao: Session, conteudo: bytes
) -> Iterator[tuple[Documento, bytes]]:
    """Um documento no Postgres com a página realmente gravada no MinIO."""
    documento_id = uuid.uuid4()
    chave = build_key(documento_id, hashlib.sha256(conteudo).hexdigest(), ".png")
    S3DocumentStorage(settings).put(chave, conteudo, "image/png")

    linha = Documento(
        id=documento_id,
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url=chave,
        competencia=COMPETENCIA_TESTE,
        status=DocumentoStatus.PROCESSANDO,
        pagina=1,
    )
    sessao.add(linha)
    sessao.commit()

    yield linha, conteudo

    sessao.execute(text("delete from documentos where id = :id"), {"id": documento_id})
    sessao.commit()
    _boto3_client(settings).delete_object(Bucket=settings.s3_bucket, Key=chave)


@pytest.fixture
def documento_sem_arquivo(sessao: Session) -> Iterator[Documento]:
    """Documento cuja chave nunca foi gravada — o arquivo que sumiu do storage."""
    linha = Documento(
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url=build_key(uuid.uuid4(), "a" * 64, ".png"),
        competencia=COMPETENCIA_TESTE,
        status=DocumentoStatus.PROCESSANDO,
        pagina=1,
    )
    sessao.add(linha)
    sessao.commit()

    yield linha

    sessao.execute(text("delete from documentos where id = :id"), {"id": linha.id})
    sessao.commit()


# --- AC1 e AC4: o arquivo sai inteiro, e é o mesmo que entrou ------------------


def test_serve_os_bytes_gravados_com_o_content_type_correto(
    api: TestClient, documento: tuple[Documento, bytes]
) -> None:
    linha, conteudo = documento

    resposta = api.get(f"/api/documentos/{linha.id}/arquivo", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    assert resposta.headers["content-type"] == "image/png"
    assert resposta.content == conteudo


def test_content_disposition_e_inline_com_nome_legivel(
    api: TestClient, documento: tuple[Documento, bytes]
) -> None:
    """Quem confere quer ver o documento ao lado da extração, não baixar arquivo."""
    linha, _conteudo = documento

    resposta = api.get(f"/api/documentos/{linha.id}/arquivo", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    disposition = resposta.headers["content-disposition"]
    assert disposition.startswith("inline;")
    assert f'filename="evolucao-{COMPETENCIA_TESTE}-pagina-1.png"' in disposition
    # O nome não pode entregar a chave do storage nem o id interno.
    assert linha.arquivo_url not in disposition


# --- AC2: os dois 404 ---------------------------------------------------------


def test_documento_inexistente_responde_404(api: TestClient) -> None:
    resposta = api.get(f"/api/documentos/{uuid.uuid4()}/arquivo", headers=AUTH_HEADERS)

    assert resposta.status_code == 404
    assert resposta.json()["error"]["tipo"] == "not_found"


def test_documento_com_arquivo_ausente_no_storage_responde_404(
    api: TestClient, documento_sem_arquivo: Documento
) -> None:
    """Arquivo que sumiu do bucket é 404, não 500: o pedido está certo — o
    objeto é que não está lá."""
    resposta = api.get(f"/api/documentos/{documento_sem_arquivo.id}/arquivo", headers=AUTH_HEADERS)

    assert resposta.status_code == 404
    assert resposta.json()["error"]["tipo"] == "not_found"


def test_storage_indisponivel_continua_503(
    api: TestClient, documento_sem_arquivo: Documento
) -> None:
    """O 404 do arquivo ausente não pode ter engolido a falha de infraestrutura."""
    app.dependency_overrides[get_document_storage] = FailingStorage

    resposta = api.get(f"/api/documentos/{documento_sem_arquivo.id}/arquivo", headers=AUTH_HEADERS)

    assert resposta.status_code == 503
    assert resposta.json()["error"]["tipo"] == "service_unavailable"


# --- AC3: credencial ----------------------------------------------------------


def test_sem_credencial_responde_401(api: TestClient, documento: tuple[Documento, bytes]) -> None:
    linha, _conteudo = documento

    resposta = api.get(f"/api/documentos/{linha.id}/arquivo")

    assert resposta.status_code == 401


# --- o que não pode aparecer no log ------------------------------------------


def test_nao_registra_a_chave_do_storage_em_log(
    api: TestClient, documento: tuple[Documento, bytes], caplog: pytest.LogCaptureFixture
) -> None:
    """A chave identifica o objeto de prontuário no bucket; o conteúdo é o prontuário.

    A asserção olha só os loggers de `homecareos`, e a exclusão é deliberada:
    em `DEBUG` o botocore registra a URL assinada do `GET`, que carrega a chave
    dentro. Isso é comportamento de biblioteca de terceiro — e mais um motivo
    para não subir a API com o log do botocore em `DEBUG` —, não algo que este
    endpoint tenha como evitar.
    """
    linha, _conteudo = documento

    with caplog.at_level(logging.DEBUG):
        resposta = api.get(f"/api/documentos/{linha.id}/arquivo", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    nossos_registros = [
        registro.getMessage()
        for registro in caplog.records
        if registro.name.startswith("homecareos")
    ]
    assert all(linha.arquivo_url not in mensagem for mensagem in nossos_registros)
