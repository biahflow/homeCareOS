"""Testes do CRUD HTTP de `regras` (issue #5) contra um Postgres real.

Cada teste roda dentro de uma transação sempre revertida no fim — não deixa
dado no Postgres compartilhado (`localhost:5434`) que a trilha F também usa.

Ponto crítico: `rules.repository` chama `session.commit()` internamente
(`criar_regra`/`atualizar_regra`/`desativar_regra`). O padrão de
`tests/test_models.py` (`Session(bind=connection)` sem `join_transaction_mode`)
deixaria esse commit interno finalizar a transação externa cedo, e o
`transaction.rollback()` do teardown viraria no-op — os dados vazariam pro
banco compartilhado. `join_transaction_mode="create_savepoint"` é o padrão
oficial do SQLAlchemy 2.0 para evitar isso: cada `commit()` interno
libera/reabre um SAVEPOINT, e só o `rollback()` do fixture desfaz tudo de
fato.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from homecareos.db.models import Operadora, Regra
from homecareos.db.session import get_engine, get_session
from homecareos.extraction.schema import EvolucaoProntuario
from homecareos.rules.engine import validar
from homecareos.rules.repository import buscar_regras_ativas, listar_regras
from homecareos.rules.router import router


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = get_engine()
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def operadora(db_session: Session) -> Operadora:
    op = Operadora(nome="Operadora Teste", codigo=f"TESTE-{uuid.uuid4()}")
    db_session.add(op)
    db_session.flush()
    return op


@pytest.fixture
def outra_operadora(db_session: Session) -> Operadora:
    op = Operadora(nome="Outra Operadora Teste", codigo=f"OUTRA-{uuid.uuid4()}")
    db_session.add(op)
    db_session.flush()
    return op


@pytest.fixture
def api(db_session: Session) -> Iterator[TestClient]:
    def override_get_session() -> Iterator[Session]:
        # Precisa ser uma função geradora de verdade (não `lambda: iter([...])`):
        # o FastAPI só trata uma dependência como "generator dependency" (e a
        # desembrulha para o valor gerado) quando o próprio callable é
        # reconhecido via `inspect.isgeneratorfunction`. Um lambda que devolve
        # um iterador não é reconhecido como tal — o handler recebe o
        # iterador em si como `session`, não a `Session`.
        yield db_session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = override_get_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _corpo_regra(operadora_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "operadora_id": str(operadora_id),
        "campo": "carimbo_legivel",
        "condicao": {"tipo": "verdadeiro"},
        "acao": "rejeitar",
        "motivo_glosa": "Carimbo ilegível",
    }
    base.update(overrides)
    return base


def test_post_regra_valida_cria_e_persiste(
    api: TestClient, db_session: Session, operadora: Operadora
) -> None:
    resposta = api.post("/api/regras", json=_corpo_regra(operadora.id))

    assert resposta.status_code == 201
    corpo = resposta.json()
    assert corpo["ativo"] is True
    assert "id" in corpo

    regra_id = uuid.UUID(corpo["id"])
    loaded = db_session.get(Regra, regra_id)
    assert loaded is not None
    assert loaded.campo == "carimbo_legivel"


def test_post_regra_condicao_desconhecida_retorna_422_e_nao_grava(
    api: TestClient, db_session: Session, operadora: Operadora
) -> None:
    antes = len(listar_regras(db_session, operadora.id))

    resposta = api.post(
        "/api/regras", json=_corpo_regra(operadora.id, condicao={"tipo": "nao_existe"})
    )

    assert resposta.status_code == 422
    depois = len(listar_regras(db_session, operadora.id))
    assert depois == antes == 0


def test_get_regras_filtra_por_operadora(
    api: TestClient,
    db_session: Session,
    operadora: Operadora,
    outra_operadora: Operadora,
) -> None:
    api.post("/api/regras", json=_corpo_regra(operadora.id, campo="carimbo_legivel"))
    api.post(
        "/api/regras",
        json=_corpo_regra(outra_operadora.id, campo="assinatura_profissional_presente"),
    )

    resposta = api.get("/api/regras", params={"operadora_id": str(operadora.id)})

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert len(corpo) == 1
    assert corpo[0]["operadora_id"] == str(operadora.id)
    assert corpo[0]["campo"] == "carimbo_legivel"


def test_put_regra_atualiza_campos(
    api: TestClient, db_session: Session, operadora: Operadora
) -> None:
    criada = api.post("/api/regras", json=_corpo_regra(operadora.id)).json()
    regra_id = criada["id"]

    resposta = api.put(
        f"/api/regras/{regra_id}",
        json=_corpo_regra(
            operadora.id,
            campo="registro_coren",
            condicao={"tipo": "formato", "regex": r"^\d{2}\.\d{3}$"},
            motivo_glosa="COREN fora do formato",
        ),
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["campo"] == "registro_coren"
    assert corpo["motivo_glosa"] == "COREN fora do formato"


def test_put_regra_id_inexistente_retorna_404(api: TestClient, operadora: Operadora) -> None:
    resposta = api.put(f"/api/regras/{uuid.uuid4()}", json=_corpo_regra(operadora.id))

    assert resposta.status_code == 404


def test_delete_regra_desativa_e_preserva_linha(
    api: TestClient, db_session: Session, operadora: Operadora
) -> None:
    criada = api.post("/api/regras", json=_corpo_regra(operadora.id)).json()
    regra_id = criada["id"]

    resposta = api.delete(f"/api/regras/{regra_id}")

    assert resposta.status_code == 200
    assert resposta.json()["ativo"] is False

    loaded = db_session.get(Regra, uuid.UUID(regra_id))
    assert loaded is not None
    assert loaded.ativo is False


def test_regra_criada_via_http_vale_sem_reiniciar_processo(
    api: TestClient, db_session: Session, operadora: Operadora
) -> None:
    """Fio-terra do critério de aceite central da issue #5: regra nova vale
    sem deploy/restart — cria via HTTP e usa no motor no mesmo teste."""
    resposta = api.post(
        "/api/regras",
        json=_corpo_regra(
            operadora.id,
            campo="assinatura_profissional_presente",
            condicao={"tipo": "verdadeiro"},
            motivo_glosa="Assinatura do profissional ausente",
        ),
    )
    assert resposta.status_code == 201

    regras_ativas = buscar_regras_ativas(db_session, operadora.id)
    campos = EvolucaoProntuario(assinatura_profissional_presente=False)

    resultados = validar(campos, regras_ativas, competencia="2024-03")

    assert len(resultados) == 1
    assert resultados[0].motivo_glosa == "Assinatura do profissional ausente"
