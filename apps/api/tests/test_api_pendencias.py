"""Testes de integração de `GET/PATCH /api/pendencias` e `GET
/api/pendencias/resumo` — contra Postgres real (localhost:5434).

Nada no sistema cria pendências ainda (issue #7 é de outra trilha) — a tabela
é, por construção, só o que este teste grava e depois apaga. Cada teste cria
seu próprio documento-âncora e suas próprias pendências (via ORM direto, não
via API — não existe endpoint de criação) e remove tudo ao final.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
from homecareos.db.models import Documento, Pendencia, PendenciaStatus, TipoDocumento
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
COMPETENCIA_TESTE = "2099-03"


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
        str(sessao.execute(text("select id from operadoras where codigo = 'UNIMED'")).scalar_one())
    )


@pytest.fixture
def documento_ancora(sessao: Session, operadora_id: uuid.UUID) -> Iterator[Documento]:
    documento = Documento(
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url="s3://fake/pendencias-teste/1",
        competencia=COMPETENCIA_TESTE,
        operadora_id=operadora_id,
    )
    sessao.add(documento)
    sessao.commit()

    yield documento

    sessao.execute(text("delete from pendencias where documento_id = :id"), {"id": documento.id})
    sessao.execute(text("delete from documentos where id = :id"), {"id": documento.id})
    sessao.commit()


@pytest.fixture
def pendencias_de_teste(sessao: Session, documento_ancora: Documento) -> list[Pendencia]:
    agora = datetime.now(UTC)
    pendencias = [
        Pendencia(
            documento_id=documento_ancora.id,
            tipo_problema="campo_ausente",
            descricao="falta assinatura do responsável técnico",
            responsavel="equipe-conferencia",
            status=PendenciaStatus.ABERTA,
            deadline=agora - timedelta(days=1),
        ),
        Pendencia(
            documento_id=documento_ancora.id,
            tipo_problema="campo_ausente",
            descricao="falta data de atendimento",
            responsavel="equipe-conferencia",
            status=PendenciaStatus.EM_CORRECAO,
            deadline=agora + timedelta(days=3),
        ),
        Pendencia(
            documento_id=documento_ancora.id,
            tipo_problema="campo_ausente",
            descricao="falta CRM do profissional",
            responsavel="equipe-conferencia",
            status=PendenciaStatus.RESOLVIDA,
            deadline=agora + timedelta(days=100),
            resolved_at=agora,
        ),
        Pendencia(
            documento_id=documento_ancora.id,
            tipo_problema="campo_ausente",
            descricao="falta anexo do laudo",
            responsavel="equipe-conferencia",
            status=PendenciaStatus.EM_CORRECAO,
            deadline=agora + timedelta(days=30),
        ),
    ]
    sessao.add_all(pendencias)
    sessao.commit()
    return pendencias


# --- autenticação -------------------------------------------------------------


def test_listar_pendencias_sem_x_api_key_responde_401(api: TestClient) -> None:
    assert api.get("/api/pendencias").status_code == 401


def test_resumo_pendencias_sem_x_api_key_responde_401(api: TestClient) -> None:
    assert api.get("/api/pendencias/resumo").status_code == 401


# --- AC5: listagem pagina e filtra -------------------------------------------


def test_listar_pendencias_filtra_por_status_e_operadora(
    api: TestClient, pendencias_de_teste: list[Pendencia], operadora_id: uuid.UUID
) -> None:
    resposta = api.get(
        f"/api/pendencias?status=aberta&operadora_id={operadora_id}", headers=AUTH_HEADERS
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["paginacao"]["total"] == 1
    assert corpo["data"][0]["status"] == "aberta"


def test_listar_pendencias_filtra_por_deadline(
    api: TestClient, pendencias_de_teste: list[Pendencia], operadora_id: uuid.UUID
) -> None:
    """`deadline` filtra pendências com deadline até (inclusive) a data informada."""
    hoje = datetime.now(UTC).date().isoformat()

    resposta = api.get(
        f"/api/pendencias?deadline={hoje}&operadora_id={operadora_id}", headers=AUTH_HEADERS
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["paginacao"]["total"] == 1
    assert corpo["data"][0]["status"] == "aberta"


def test_listar_pendencias_filtra_por_operadora_traz_todas(
    api: TestClient, pendencias_de_teste: list[Pendencia], operadora_id: uuid.UUID
) -> None:
    resposta = api.get(f"/api/pendencias?operadora_id={operadora_id}", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    assert resposta.json()["paginacao"]["total"] == len(pendencias_de_teste)


# --- AC6: transição de status -------------------------------------------------


def test_transicao_aberta_para_em_correcao_e_valida(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    aberta = next(p for p in pendencias_de_teste if p.status == PendenciaStatus.ABERTA)

    resposta = api.patch(
        f"/api/pendencias/{aberta.id}", json={"status": "em_correcao"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 200
    assert resposta.json()["status"] == "em_correcao"


def test_transicao_em_correcao_para_resolvida_preenche_resolved_at(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    em_correcao = next(p for p in pendencias_de_teste if p.status == PendenciaStatus.EM_CORRECAO)

    resposta = api.patch(
        f"/api/pendencias/{em_correcao.id}", json={"status": "resolvida"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == "resolvida"
    assert corpo["resolved_at"] is not None


def test_transicao_pulando_etapa_responde_422(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    aberta = next(p for p in pendencias_de_teste if p.status == PendenciaStatus.ABERTA)

    resposta = api.patch(
        f"/api/pendencias/{aberta.id}", json={"status": "resolvida"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 422


def test_transicao_para_tras_responde_422(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    resolvida = next(p for p in pendencias_de_teste if p.status == PendenciaStatus.RESOLVIDA)

    resposta = api.patch(
        f"/api/pendencias/{resolvida.id}", json={"status": "aberta"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 422


def test_atualizar_pendencia_inexistente_responde_404(api: TestClient) -> None:
    resposta = api.patch(
        f"/api/pendencias/{uuid.uuid4()}", json={"status": "em_correcao"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 404


# --- resumo por status e faixa de deadline ------------------------------------


def test_resumo_pendencias_conta_por_status_e_faixa_deadline(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    resposta = api.get("/api/pendencias/resumo", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert set(corpo["por_status"]) == {"aberta", "em_correcao", "resolvida"}
    assert corpo["por_status"]["aberta"] >= 1
    assert corpo["por_status"]["em_correcao"] >= 1
    assert corpo["por_status"]["resolvida"] >= 1
    assert set(corpo["por_faixa_deadline"]) == {"vencidas", "proximos_7_dias", "futuras"}
    assert corpo["por_faixa_deadline"]["vencidas"] >= 1
    assert corpo["por_faixa_deadline"]["proximos_7_dias"] >= 1
    assert corpo["por_faixa_deadline"]["futuras"] >= 1
