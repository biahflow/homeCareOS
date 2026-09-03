"""Testes de integração de `GET /api/documentos`, `GET /api/documentos/{id}` e
`POST /api/documentos/{id}/revalidar` — contra Postgres real (localhost:5434).

O banco é compartilhado com outras trilhas rodando em paralelo, então cada
teste cria só os documentos de que precisa sob `COMPETENCIA_TESTE` (um valor
que nenhum dado real usaria) e apaga tudo o que criou ao final.

O contrato de `POST /api/documentos` (`{"documentos": [{"id", "pagina",
"status", "competencia"}]}`) já é travado por
`tests/test_intake_router.py::test_pdf_de_dez_paginas_responde_201_com_dez_documentos`
e exercitado contra Postgres/MinIO reais por `tests/test_e2e_upload.py` — não
duplicado aqui.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    Extracao,
    Regra,
    ResultadoValidacao,
    TipoDocumento,
    Validacao,
)
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
COMPETENCIA_TESTE = "2099-01"
"""Competência que nenhum dado real usaria — isola os testes desta suíte de
qualquer outro dado no Postgres compartilhado."""


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
def operadora_id(sessao: Session) -> uuid.UUID:
    return uuid.UUID(
        str(sessao.execute(text("select id from operadoras where codigo = 'AMIL'")).scalar_one())
    )


@pytest.fixture
def documentos_de_teste(sessao: Session, operadora_id: uuid.UUID) -> Iterator[list[Documento]]:
    documentos = [
        Documento(
            tipo=TipoDocumento.EVOLUCAO,
            arquivo_url="s3://fake/documentos-teste/1",
            competencia=COMPETENCIA_TESTE,
            status=DocumentoStatus.PROCESSANDO,
            pagina=1,
        ),
        Documento(
            tipo=TipoDocumento.EVOLUCAO,
            arquivo_url="s3://fake/documentos-teste/2",
            competencia=COMPETENCIA_TESTE,
            status=DocumentoStatus.APROVADO,
            pagina=2,
            operadora_id=operadora_id,
        ),
        Documento(
            tipo=TipoDocumento.EVOLUCAO,
            arquivo_url="s3://fake/documentos-teste/3",
            competencia=COMPETENCIA_TESTE,
            status=DocumentoStatus.PROBLEMA,
            pagina=3,
        ),
    ]
    sessao.add_all(documentos)
    sessao.commit()
    ids = [documento.id for documento in documentos]

    yield documentos

    sessao.execute(text("delete from validacoes where documento_id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from extracoes where documento_id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from documentos where id = any(:ids)"), {"ids": ids})
    sessao.commit()


# --- AC1: sem chave, nenhuma rota nova responde ------------------------------


def test_listar_documentos_sem_x_api_key_responde_401(api: TestClient) -> None:
    resposta = api.get(f"/api/documentos?competencia={COMPETENCIA_TESTE}")

    assert resposta.status_code == 401


# --- AC5: listagem pagina e filtra -------------------------------------------


def test_listar_documentos_filtra_por_competencia(
    api: TestClient, documentos_de_teste: list[Documento]
) -> None:
    resposta = api.get(f"/api/documentos?competencia={COMPETENCIA_TESTE}", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["paginacao"] == {"total": 3, "limite": 50, "offset": 0}
    assert {item["status"] for item in corpo["data"]} == {"processando", "aprovado", "problema"}


def test_listar_documentos_status_processando_so_traz_esses(
    api: TestClient, documentos_de_teste: list[Documento]
) -> None:
    resposta = api.get(
        f"/api/documentos?competencia={COMPETENCIA_TESTE}&status=processando",
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["paginacao"]["total"] == 1
    assert all(item["status"] == "processando" for item in corpo["data"])


def test_listar_documentos_filtra_por_operadora(
    api: TestClient, documentos_de_teste: list[Documento], operadora_id: uuid.UUID
) -> None:
    resposta = api.get(
        f"/api/documentos?competencia={COMPETENCIA_TESTE}&operadora_id={operadora_id}",
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["paginacao"]["total"] == 1
    assert corpo["data"][0]["status"] == "aprovado"


def test_listar_documentos_respeita_limite_e_offset(
    api: TestClient, documentos_de_teste: list[Documento]
) -> None:
    resposta = api.get(
        f"/api/documentos?competencia={COMPETENCIA_TESTE}&limite=2&offset=1",
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["paginacao"] == {"total": 3, "limite": 2, "offset": 1}
    assert len(corpo["data"]) == 2


# --- detalhe: extração e validações ------------------------------------------


def test_obter_documento_inexistente_responde_404(api: TestClient) -> None:
    resposta = api.get(f"/api/documentos/{uuid.uuid4()}", headers=AUTH_HEADERS)

    assert resposta.status_code == 404


def test_obter_documento_sem_extracao_nem_validacao(
    api: TestClient, documentos_de_teste: list[Documento]
) -> None:
    documento = documentos_de_teste[0]

    resposta = api.get(f"/api/documentos/{documento.id}", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["id"] == str(documento.id)
    assert corpo["extracao"] is None
    assert corpo["validacoes"] == []


def test_obter_documento_traz_extracao_e_validacoes(
    api: TestClient,
    sessao: Session,
    documentos_de_teste: list[Documento],
    operadora_id: uuid.UUID,
) -> None:
    documento = documentos_de_teste[0]
    extracao = Extracao(
        documento_id=documento.id,
        campos_extraidos={"nome_paciente": "Maria da Silva"},
        confianca=0.92,
        confianca_por_campo={"nome_paciente": 0.92},
        modelo="modelo-de-teste",
        provider="teste",
    )
    regra = Regra(
        operadora_id=operadora_id,
        campo="nome_paciente",
        condicao="obrigatorio",
        acao="glosar",
        motivo_glosa="campo obrigatório ausente",
    )
    sessao.add_all([extracao, regra])
    sessao.commit()
    validacao = Validacao(
        documento_id=documento.id,
        regra_id=regra.id,
        resultado=ResultadoValidacao.APROVADO,
        detalhe="campo presente",
    )
    sessao.add(validacao)
    sessao.commit()

    try:
        resposta = api.get(f"/api/documentos/{documento.id}", headers=AUTH_HEADERS)

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["extracao"]["campos_extraidos"] == {"nome_paciente": "Maria da Silva"}
        assert len(corpo["validacoes"]) == 1
        assert corpo["validacoes"][0]["resultado"] == "aprovado"
    finally:
        sessao.execute(text("delete from validacoes where id = :id"), {"id": validacao.id})
        sessao.execute(text("delete from regras where id = :id"), {"id": regra.id})
        sessao.commit()


# --- POST /revalidar: stub honesto (issue #5 é de outra trilha) -------------


def test_revalidar_responde_501_com_mensagem_honesta(api: TestClient) -> None:
    resposta = api.post(f"/api/documentos/{uuid.uuid4()}/revalidar", headers=AUTH_HEADERS)

    assert resposta.status_code == 501
    corpo = resposta.json()
    mensagem = corpo["error"]["mensagem"].lower()
    assert "regra" in mensagem
    # nunca finge sucesso: não pode haver um corpo que pareça resultado de validação.
    assert "aprovado" not in mensagem
    assert "reprovado" not in mensagem
