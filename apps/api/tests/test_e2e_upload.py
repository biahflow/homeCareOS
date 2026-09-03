"""E2E do upload contra Postgres e MinIO reais (`docker compose up -d`).

Cobre os critérios de aceite 1 a 4 da Fase 2 com infraestrutura de verdade:
documentos gravados no Postgres, páginas gravadas no MinIO sob a chave de
`build_key()`, `extracoes` com `raw_response_ref` apontando para o objeto JSON,
e reenvio com a mesma `Idempotency-Key` devolvendo 200 sem criar nada novo.

O que **não** é real aqui é a chamada ao modelo de visão: um provider de teste
devolve um resultado fixo e persiste o payload cru pelo `S3RawResponseStore`
real. O objetivo é exercitar a integração com a infraestrutura, não gastar
chamada paga de API dentro da suíte.

Pré-requisitos (o teste é pulado quando faltam):

    docker compose up -d
    docker compose run --rm api-migrate
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
from homecareos.db.session import get_engine, get_sessionmaker
from homecareos.extraction.dispatcher import SyncExtractionDispatcher
from homecareos.extraction.s3_raw_store import S3RawResponseStore
from homecareos.extraction.schema import (
    EvolucaoProntuario,
    ExtractionResult,
    PaginaDocumento,
)
from homecareos.intake.router import get_document_storage, get_extraction_dispatcher
from homecareos.main import app
from homecareos.storage import S3DocumentStorage, build_key
from tests.fakes import make_pdf

pytestmark = pytest.mark.integration

COMPETENCIA = "2024-03"
PAGINAS = 10
SONDA_TIMEOUT = 2
"""Segundos de paciência das sondas de disponibilidade."""


def _postgres_responde(settings: Settings) -> str | None:
    """`None` quando o Postgres responde; senão, o motivo da indisponibilidade.

    Um `connect` de socket não serve como sonda: com o Docker Desktop parado, o
    proxy dele continua aceitando conexão em 5434/9002 e nada responde atrás.
    A sonda tem que exigir resposta do serviço, com prazo curto.
    """
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


def _minio_responde(settings: Settings, client: Any) -> str | None:
    """`None` quando o bucket existe e responde; senão, o motivo."""
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


@dataclass
class ProviderDeTeste:
    """Provider determinístico que persiste o raw response no storage real."""

    raw_store: S3RawResponseStore
    name: str = "teste"
    chamadas: list[str] = field(default_factory=list)

    def extract(self, pagina: PaginaDocumento, documento_id: str | None = None) -> ExtractionResult:
        self.chamadas.append(documento_id or "")
        raw_response: dict[str, Any] = {"pagina": pagina.numero, "stop_reason": "end_turn"}
        chave = self.raw_store.persist(documento_id or "sem-id", raw_response)
        return ExtractionResult(
            campos=EvolucaoProntuario(nome_paciente="Maria da Silva"),
            confianca=1.0,
            confianca_por_campo={"nome_paciente": 1.0},
            raw_response=raw_response,
            modelo="modelo-de-teste",
            provider=self.name,
            raw_response_key=chave,
        )


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


def _boto3_client(settings: Settings) -> Any:
    """Cliente próprio do teste, para inspecionar o bucket sem passar pelo
    storage sob teste (nem pelos seus atributos privados)."""
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


@dataclass
class StorageQueAnota:
    """Storage real que anota toda chave gravada, para a limpeza não perder nada.

    Ler as chaves de volta em `documentos` no fim do teste não basta: o reenvio
    idempotente grava as páginas no bucket **antes** de o banco recusar os
    documentos, e esses objetos ficam sem linha que os referencie. A limpeza
    tem que saber o que foi escrito, não o que sobrou registrado.
    """

    interno: S3DocumentStorage
    gravadas: list[str] = field(default_factory=list)

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.gravadas.append(key)
        return self.interno.put(key, data, content_type)

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return self.interno.presigned_url(key, expires_in)


@pytest.fixture
def storage(settings: Settings) -> StorageQueAnota:
    return StorageQueAnota(interno=S3DocumentStorage(settings))


@pytest.fixture
def s3(settings: Settings) -> Any:
    return _boto3_client(settings)


@pytest.fixture
def provider(storage: StorageQueAnota) -> ProviderDeTeste:
    return ProviderDeTeste(raw_store=S3RawResponseStore(storage))


@pytest.fixture
def api(
    settings: Settings, storage: StorageQueAnota, provider: ProviderDeTeste
) -> Iterator[TestClient]:
    dispatcher = SyncExtractionDispatcher(provider=provider, session_factory=get_sessionmaker())
    app.dependency_overrides[get_document_storage] = lambda: storage
    app.dependency_overrides[get_extraction_dispatcher] = lambda: dispatcher
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def limpeza(settings: Settings, s3: Any, storage: StorageQueAnota) -> Iterator[list[uuid.UUID]]:
    """Remove do banco e do bucket tudo o que o teste criou."""
    criados: list[uuid.UUID] = []
    yield criados
    if criados:
        engine = get_engine()
        with engine.begin() as conexao:
            for tabela in ("extracoes", "log_conferencia"):
                conexao.execute(
                    text(f"delete from {tabela} where documento_id = any(:ids)"),
                    {"ids": criados},
                )
            conexao.execute(text("delete from documentos where id = any(:ids)"), {"ids": criados})
    for chave in storage.gravadas:
        s3.delete_object(Bucket=settings.s3_bucket, Key=chave)


def _upload(api: TestClient, conteudo: bytes, chave: str) -> Any:
    return api.post(
        "/api/documentos",
        files={"arquivo": ("evolucao.pdf", conteudo, "application/pdf")},
        data={"competencia": COMPETENCIA},
        headers={"Idempotency-Key": chave},
    )


def test_upload_ponta_a_ponta_com_postgres_e_minio_reais(
    api: TestClient,
    sessao: Session,
    s3: Any,
    provider: ProviderDeTeste,
    settings: Settings,
    limpeza: list[uuid.UUID],
) -> None:
    conteudo = make_pdf(PAGINAS)
    chave_idempotencia = f"e2e-{uuid.uuid4()}"

    resposta = _upload(api, conteudo, chave_idempotencia)

    # AC1: 201 com uma linha por página, todas em `processando`.
    assert resposta.status_code == 201, resposta.text
    documentos = resposta.json()["documentos"]
    assert len(documentos) == PAGINAS
    assert [d["pagina"] for d in documentos] == list(range(1, PAGINAS + 1))
    assert all(d["status"] == "processando" for d in documentos)
    ids = [uuid.UUID(d["id"]) for d in documentos]
    limpeza.extend(ids)

    linhas = sessao.execute(
        text(
            "select id, arquivo_url, status, pagina, competencia, idempotency_key "
            "from documentos where id = any(:ids) order by pagina"
        ),
        {"ids": ids},
    ).all()
    assert len(linhas) == PAGINAS
    assert [linha.idempotency_key for linha in linhas] == [
        f"{chave_idempotencia}:{numero}" for numero in range(1, PAGINAS + 1)
    ]

    # AC2: os objetos existem no MinIO e a chave bate com `build_key()` sobre o
    # sha256 do conteúdo realmente gravado (a página renderizada, não o PDF).
    sha_do_pdf_original = hashlib.sha256(conteudo).hexdigest()
    for linha in linhas:
        objeto = s3.get_object(Bucket=settings.s3_bucket, Key=linha.arquivo_url)
        gravado = objeto["Body"].read()
        sha_gravado = hashlib.sha256(gravado).hexdigest()
        assert linha.arquivo_url == build_key(linha.id, sha_gravado, ".png")
        assert sha_gravado != sha_do_pdf_original
        assert gravado.startswith(b"\x89PNG\r\n\x1a\n")

    # AC3: uma extração por documento, com o raw response no object storage.
    extracoes = sessao.execute(
        text("select documento_id, raw_response_ref from extracoes where documento_id = any(:ids)"),
        {"ids": ids},
    ).all()
    assert len(extracoes) == PAGINAS
    for extracao in extracoes:
        assert extracao.raw_response_ref is not None
        assert extracao.raw_response_ref.startswith(f"extracoes/{extracao.documento_id}/")
        assert extracao.raw_response_ref.endswith(".json")
        s3.head_object(Bucket=settings.s3_bucket, Key=extracao.raw_response_ref)

    # AC4: reenvio com a mesma chave devolve 200, os mesmos ids, e não chama a
    # extração de novo.
    chamadas_antes = len(provider.chamadas)
    reenvio = _upload(api, conteudo, chave_idempotencia)

    assert reenvio.status_code == 200, reenvio.text
    assert [d["id"] for d in reenvio.json()["documentos"]] == [d["id"] for d in documentos]
    assert len(provider.chamadas) == chamadas_antes

    total = sessao.execute(
        text("select count(*) from documentos where idempotency_key like :prefixo"),
        {"prefixo": f"{chave_idempotencia}:%"},
    ).scalar_one()
    assert total == PAGINAS
