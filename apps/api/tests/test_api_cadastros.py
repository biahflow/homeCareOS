"""Testes de integração de `GET /api/operadoras`, `GET /api/pacientes` e
`POST /api/pacientes` — contra Postgres real (localhost:5434).

Operadoras já vêm seedadas (`homecareos.seed`, rodado pela Trilha A); estes
testes só leem essa tabela, nunca escrevem nela. Todo paciente criado pelo
teste é removido ao final.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY, TEST_API_KEY_PAPEIS

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2


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
        update={"api_keys": TEST_API_KEY, "api_key_papeis": TEST_API_KEY_PAPEIS}
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


# --- autenticação -------------------------------------------------------------


def test_operadoras_sem_x_api_key_responde_401(api: TestClient) -> None:
    assert api.get("/api/operadoras").status_code == 401


def test_pacientes_sem_x_api_key_responde_401(api: TestClient) -> None:
    corpo = {"nome": "x", "operadora_id": str(uuid.uuid4())}

    assert api.get("/api/pacientes").status_code == 401
    assert api.post("/api/pacientes", json=corpo).status_code == 401


# --- operadoras -----------------------------------------------------------------


def test_listar_operadoras_traz_as_seedadas(api: TestClient) -> None:
    resposta = api.get("/api/operadoras", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    codigos = {operadora["codigo"] for operadora in resposta.json()}
    assert {"AMIL", "UNIMED"} <= codigos


# --- pacientes --------------------------------------------------------------


def test_criar_paciente_e_lista_lo_filtrando_por_operadora(
    api: TestClient, sessao: Session, operadora_id: uuid.UUID
) -> None:
    nome_marcador = f"Paciente de teste {uuid.uuid4()}"

    resposta = api.post(
        "/api/pacientes",
        json={
            "nome": nome_marcador,
            "operadora_id": str(operadora_id),
            "modalidade": "AD",
        },
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 201
    corpo = resposta.json()
    assert corpo["nome"] == nome_marcador
    assert corpo["modalidade"] == "AD"
    paciente_id = corpo["id"]

    try:
        listagem = api.get(f"/api/pacientes?operadora_id={operadora_id}", headers=AUTH_HEADERS)
        assert listagem.status_code == 200
        ids_listados = {item["id"] for item in listagem.json()["data"]}
        assert paciente_id in ids_listados
    finally:
        sessao.execute(text("delete from pacientes where id = :id"), {"id": paciente_id})
        sessao.commit()


def test_criar_paciente_com_operadora_inexistente_responde_422(api: TestClient) -> None:
    resposta = api.post(
        "/api/pacientes",
        json={"nome": "Fulano de Tal", "operadora_id": str(uuid.uuid4()), "modalidade": "ID"},
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 422


def test_criar_paciente_com_modalidade_invalida_responde_422(
    api: TestClient, operadora_id: uuid.UUID
) -> None:
    resposta = api.post(
        "/api/pacientes",
        json={
            "nome": "Fulano de Tal",
            "operadora_id": str(operadora_id),
            "modalidade": "não-existe",
        },
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 422
