"""Testes de integração de `seed_regras()` (issue #10) — contra Postgres real (localhost:5434).

O banco é compartilhado com o desenvolvimento e com os outros testes de
integração: cada teste cria a sua própria operadora com código único e limpa
só o que criou. `seed_regras()` materializa o catálogo TISS para **todas** as
operadoras do banco — inclusive as seedadas de verdade (AMIL, UNIMED, ...) —
e essas linhas não são apagadas no teardown: elas são o estado esperado do
banco depois do seed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
from homecareos.db.models import Operadora, Regra
from homecareos.db.session import get_sessionmaker
from homecareos.rules.catalogo import carregar_tiss
from homecareos.rules.repository import buscar_regras_ativas
from homecareos.rules.schema import CondicaoTypeAdapter
from homecareos.rules.seed_regras import seed_regras

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
def sessao(settings: Settings) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def operadora_teste(sessao: Session) -> Iterator[Operadora]:
    operadora = Operadora(nome="Operadora de teste do seed", codigo=f"SEED-{uuid.uuid4()}")
    sessao.add(operadora)
    sessao.commit()

    yield operadora

    sessao.execute(text("delete from regras where operadora_id = :id"), {"id": operadora.id})
    sessao.execute(text("delete from operadoras where id = :id"), {"id": operadora.id})
    sessao.commit()


@pytest.fixture
def operadora_amil_id(sessao: Session) -> uuid.UUID:
    return uuid.UUID(
        str(sessao.execute(text("select id from operadoras where codigo = 'AMIL'")).scalar_one())
    )


def _regras_da_operadora(sessao: Session, operadora_id: uuid.UUID) -> list[Regra]:
    stmt = select(Regra).where(Regra.operadora_id == operadora_id).order_by(Regra.codigo)
    return list(sessao.scalars(stmt))


def test_seed_cria_as_treze_genericas_para_a_operadora_de_teste(
    sessao: Session, operadora_teste: Operadora
) -> None:
    seed_regras()
    sessao.expire_all()

    regras = _regras_da_operadora(sessao, operadora_teste.id)

    assert len(regras) == 13
    for regra in regras:
        assert regra.escopo == "tiss"
        assert regra.ativo is True
        assert regra.fonte is not None
        assert regra.codigo is not None


def test_operadora_amil_recebe_tambem_as_seis_especificas_inativas(
    sessao: Session, operadora_amil_id: uuid.UUID
) -> None:
    seed_regras()
    sessao.expire_all()

    # Filtra por `codigo is not None`, não só por `escopo == "operadora"`: o
    # banco é compartilhado com outros testes/dev, que podem ter criado regra
    # de API para a AMIL (sempre sem `codigo`, por definição) e ela não é do
    # catálogo — contá-la aqui tornaria o teste dependente de estado alheio.
    especificas = [
        regra
        for regra in _regras_da_operadora(sessao, operadora_amil_id)
        if regra.escopo == "operadora" and regra.codigo is not None
    ]

    assert len(especificas) == 6
    assert all(regra.ativo is False for regra in especificas)


def test_seed_e_idempotente(sessao: Session, operadora_teste: Operadora) -> None:
    seed_regras()
    sessao.expire_all()
    contagem_apos_primeira = len(_regras_da_operadora(sessao, operadora_teste.id))

    seed_regras()
    sessao.expire_all()
    contagem_apos_segunda = len(_regras_da_operadora(sessao, operadora_teste.id))

    assert contagem_apos_primeira == contagem_apos_segunda


def test_seed_nao_reativa_regra_desativada_pela_operacao(
    sessao: Session, operadora_teste: Operadora
) -> None:
    seed_regras()
    sessao.expire_all()
    alguma_regra = _regras_da_operadora(sessao, operadora_teste.id)[0]

    sessao.execute(text("update regras set ativo = false where id = :id"), {"id": alguma_regra.id})
    sessao.commit()

    seed_regras()
    sessao.expire_all()

    regra_relida = sessao.get(Regra, alguma_regra.id)
    assert regra_relida is not None
    assert regra_relida.ativo is False


def test_condicao_gravada_e_lida_de_volta_pelo_type_adapter(
    sessao: Session, operadora_teste: Operadora
) -> None:
    seed_regras()
    sessao.expire_all()

    for regra in _regras_da_operadora(sessao, operadora_teste.id):
        CondicaoTypeAdapter.validate_json(regra.condicao)


def test_buscar_regras_ativas_devolve_so_as_genericas(
    sessao: Session, operadora_teste: Operadora
) -> None:
    seed_regras()
    sessao.expire_all()

    ativas = buscar_regras_ativas(sessao, operadora_teste.id)

    codigos_tiss = {regra.codigo for regra in carregar_tiss()}
    assert len(ativas) == 13
    assert {regra.codigo for regra in ativas} == codigos_tiss
    assert all(regra.escopo == "tiss" for regra in ativas)
