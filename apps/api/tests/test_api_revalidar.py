"""Testes de integração de `POST /api/documentos/{id}/revalidar` — Postgres real.

Não dá para rodar dentro de uma transação revertida como
`test_classification_service.py`: a app real resolve `get_session` pelo
`sessionmaker` do processo, e a requisição do `TestClient` enxergaria outra
conexão. Então cada teste cria os seus próprios dados (operadora com código
único, competência que nenhum dado real usaria) e apaga tudo no teardown — o
banco `localhost:5434` é compartilhado com o desenvolvimento.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    Extracao,
    Operadora,
    Regra,
    TipoDocumento,
)
from homecareos.db.session import get_sessionmaker
from homecareos.extraction.schema import EvolucaoProntuario
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
COMPETENCIA_TESTE = "2099-07"


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


@dataclass
class Cenario:
    """Os objetos que cada teste precisa, sem repetir o setup em todos eles."""

    operadora: Operadora
    regra: Regra
    documento: Documento
    sem_operadora: Documento


@pytest.fixture(scope="module")
def settings() -> Settings:
    resolved = get_settings()
    motivo = _postgres_responde(resolved)
    if motivo is not None:
        pytest.skip(f"Postgres indisponível em {resolved.database_url}: {motivo}")
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
def cenario(sessao: Session) -> Iterator[Cenario]:
    """Operadora + regra ativa + documento; a extração fica a cargo de cada teste."""
    operadora = Operadora(nome="Operadora Revalidar", codigo=f"REVAL-{uuid.uuid4()}")
    sessao.add(operadora)
    sessao.flush()
    regra = Regra(
        operadora_id=operadora.id,
        campo="carimbo_legivel",
        condicao=json.dumps({"tipo": "verdadeiro"}),
        acao="rejeitar",
        motivo_glosa="Carimbo ilegível",
    )
    documento = Documento(
        operadora_id=operadora.id,
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url="s3://fake/revalidar-teste",
        competencia=COMPETENCIA_TESTE,
        status=DocumentoStatus.PROCESSANDO,
    )
    sem_operadora = Documento(
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url="s3://fake/revalidar-teste-sem-operadora",
        competencia=COMPETENCIA_TESTE,
        status=DocumentoStatus.PROCESSANDO,
    )
    sessao.add_all([regra, documento, sem_operadora])
    sessao.commit()

    yield Cenario(
        operadora=operadora, regra=regra, documento=documento, sem_operadora=sem_operadora
    )

    ids = [documento.id, sem_operadora.id]
    for tabela in ("pendencias", "validacoes", "extracoes", "log_conferencia"):
        sessao.execute(text(f"delete from {tabela} where documento_id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from documentos where id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from regras where operadora_id = :id"), {"id": operadora.id})
    sessao.execute(text("delete from operadoras where id = :id"), {"id": operadora.id})
    sessao.commit()


def _extracao(sessao: Session, documento_id: uuid.UUID, *, carimbo_legivel: bool) -> None:
    campos = EvolucaoProntuario(carimbo_legivel=carimbo_legivel)
    sessao.add(
        Extracao(
            documento_id=documento_id,
            campos_extraidos=campos.model_dump(mode="json"),
            confianca=0.9,
            confianca_por_campo={},
            modelo="modelo-teste",
            provider="teste",
        )
    )
    sessao.commit()


# --- autenticação -------------------------------------------------------------


def test_revalidar_sem_x_api_key_responde_401(api: TestClient) -> None:
    assert api.post(f"/api/documentos/{uuid.uuid4()}/revalidar").status_code == 401


# --- caminho feliz ------------------------------------------------------------


def test_revalidar_documento_reprovado_responde_200_com_bucket_e_pendencias(
    api: TestClient, sessao: Session, cenario: Cenario
) -> None:
    _extracao(sessao, cenario.documento.id, carimbo_legivel=False)

    resposta = api.post(f"/api/documentos/{cenario.documento.id}/revalidar", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["documento_id"] == str(cenario.documento.id)
    assert corpo["status"] == "incompleto"
    assert corpo["pendencias_abertas"] == 1


def test_revalidar_documento_aprovado_responde_200_sem_pendencia(
    api: TestClient, sessao: Session, cenario: Cenario
) -> None:
    _extracao(sessao, cenario.documento.id, carimbo_legivel=True)

    resposta = api.post(f"/api/documentos/{cenario.documento.id}/revalidar", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == "aprovado"
    assert corpo["pendencias_abertas"] == 0


# --- erros --------------------------------------------------------------------


def test_revalidar_documento_inexistente_responde_404(api: TestClient) -> None:
    resposta = api.post(f"/api/documentos/{uuid.uuid4()}/revalidar", headers=AUTH_HEADERS)

    assert resposta.status_code == 404


def test_revalidar_documento_sem_extracao_responde_409(api: TestClient, cenario: Cenario) -> None:
    resposta = api.post(f"/api/documentos/{cenario.documento.id}/revalidar", headers=AUTH_HEADERS)

    assert resposta.status_code == 409
    assert "extração" in resposta.json()["error"]["mensagem"]


def test_revalidar_documento_sem_operadora_responde_409(api: TestClient, cenario: Cenario) -> None:
    resposta = api.post(
        f"/api/documentos/{cenario.sem_operadora.id}/revalidar", headers=AUTH_HEADERS
    )

    assert resposta.status_code == 409
    assert "operadora" in resposta.json()["error"]["mensagem"]


def test_revalidar_documento_ja_aprovado_responde_409_sem_gravar_validacoes(
    api: TestClient, sessao: Session, cenario: Cenario
) -> None:
    """`aprovado` é terminal, e a recusa não pode deixar rastro em `validacoes`.

    A ordem importa: `registrar_validacoes` commita sozinho, então recusar só na
    transição encheria `GET /api/documentos/{id}` de validações vindas de
    requisições que a API rejeitou.
    """
    _extracao(sessao, cenario.documento.id, carimbo_legivel=False)
    sessao.execute(
        text("update documentos set status = 'aprovado' where id = :id"),
        {"id": cenario.documento.id},
    )
    sessao.commit()

    for _ in range(2):
        resposta = api.post(
            f"/api/documentos/{cenario.documento.id}/revalidar", headers=AUTH_HEADERS
        )
        assert resposta.status_code == 409

    validacoes = sessao.execute(
        text("select count(*) from validacoes where documento_id = :id"),
        {"id": cenario.documento.id},
    ).scalar_one()
    assert validacoes == 0


# --- reconciliação vista pela API ---------------------------------------------


def test_revalidar_duas_vezes_nao_duplica_a_pendencia(
    api: TestClient, sessao: Session, cenario: Cenario
) -> None:
    """`POST /revalidar` é público e repetível — repetir não pode multiplicar pendência."""
    _extracao(sessao, cenario.documento.id, carimbo_legivel=False)

    primeira = api.post(f"/api/documentos/{cenario.documento.id}/revalidar", headers=AUTH_HEADERS)
    segunda = api.post(f"/api/documentos/{cenario.documento.id}/revalidar", headers=AUTH_HEADERS)

    assert primeira.json()["pendencias_abertas"] == 1
    assert segunda.json()["pendencias_abertas"] == 1
    total = sessao.execute(
        text("select count(*) from pendencias where documento_id = :id"),
        {"id": cenario.documento.id},
    ).scalar_one()
    assert total == 1


def test_documento_liberado_nunca_carrega_pendencia_aberta(
    api: TestClient, sessao: Session, cenario: Cenario
) -> None:
    """A regra passou a não se aplicar: a pendência fecha junto com a liberação."""
    _extracao(sessao, cenario.documento.id, carimbo_legivel=False)
    api.post(f"/api/documentos/{cenario.documento.id}/revalidar", headers=AUTH_HEADERS)

    # A regra vira condicional e deixa de se aplicar a este documento (o carimbo
    # nem está presente), então nada mais reprova `carimbo_legivel`.
    sessao.execute(
        text("update regras set condicao = :c where operadora_id = :id"),
        {
            "c": json.dumps(
                {
                    "tipo": "se",
                    "quando": {"tipo": "verdadeiro", "campo": "carimbo_presente"},
                    "entao": {"tipo": "verdadeiro"},
                }
            ),
            "id": cenario.operadora.id,
        },
    )
    sessao.commit()

    resposta = api.post(f"/api/documentos/{cenario.documento.id}/revalidar", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    assert resposta.json()["status"] == "liberado"
    assert resposta.json()["pendencias_abertas"] == 0
    abertas = sessao.execute(
        text("select count(*) from pendencias where documento_id = :id and status != 'resolvida'"),
        {"id": cenario.documento.id},
    ).scalar_one()
    assert abertas == 0
