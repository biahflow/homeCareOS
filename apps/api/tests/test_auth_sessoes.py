"""Testes de integração da sessão de usuário — contra Postgres real (localhost:5434).

O banco é compartilhado com o desenvolvimento: cada fixture cria usuário com
e-mail único, apaga todas as sessões que criou e apaga o usuário no fim. Nenhuma
asserção conta linhas sem filtrar pelos próprios registros.

Nenhum teste daqui imprime senha nem token.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from homecareos.auth import senhas, sessoes
from homecareos.auth.schema import Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import Sessao, Usuario
from homecareos.db.session import get_sessionmaker

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-sessoes"


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
def usuario(sessao: Session) -> Iterator[Usuario]:
    linha = Usuario(
        nome="Usuário de Teste de Sessão",
        email=f"sessao-{uuid.uuid4()}@teste.local",
        senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
        papel=Papel.CONFERENTE.value,
    )
    sessao.add(linha)
    sessao.commit()

    yield linha

    # Sessões antes do usuário: a FK de `sessoes.usuario_id` não deixa a ordem
    # inversa passar.
    sessao.execute(text("delete from sessoes where usuario_id = :id"), {"id": linha.id})
    sessao.execute(text("delete from usuarios where id = :id"), {"id": linha.id})
    sessao.commit()


def test_criar_sessao_guarda_o_hash_e_nunca_o_token(sessao: Session, usuario: Usuario) -> None:
    """O critério que sustenta "um dump de banco não entrega sessão utilizável"."""
    criada, token = sessoes.criar_sessao(sessao, usuario, duracao_horas=12, agora=datetime.now(UTC))

    assert token
    assert criada.token_hash != token
    assert criada.token_hash == sessoes.hash_do_token(token)

    # O token não aparece em NENHUMA coluna da linha gravada — a asserção é
    # sobre a linha inteira lida de volta do banco, não sobre o objeto em
    # memória.
    linha = (
        sessao.execute(text("select * from sessoes where id = :id"), {"id": criada.id})
        .mappings()
        .one()
    )
    assert token not in " ".join(str(valor) for valor in linha.values())


def test_resolver_sessao_devolve_o_usuario_com_token_valido(
    sessao: Session, usuario: Usuario
) -> None:
    agora = datetime.now(UTC)
    _, token = sessoes.criar_sessao(sessao, usuario, duracao_horas=12, agora=agora)

    resolvido = sessoes.resolver_sessao(sessao, token, agora=agora)

    assert resolvido is not None
    assert resolvido.id == usuario.id


def test_sessao_expirada_nao_resolve(sessao: Session, usuario: Usuario) -> None:
    agora = datetime.now(UTC)
    _, token = sessoes.criar_sessao(sessao, usuario, duracao_horas=12, agora=agora)

    assert sessoes.resolver_sessao(sessao, token, agora=agora + timedelta(hours=13)) is None


def test_sessao_revogada_nao_resolve(sessao: Session, usuario: Usuario) -> None:
    agora = datetime.now(UTC)
    _, token = sessoes.criar_sessao(sessao, usuario, duracao_horas=12, agora=agora)

    sessoes.revogar(sessao, token, agora=agora)

    assert sessoes.resolver_sessao(sessao, token, agora=agora) is None


def test_usuario_desativado_derruba_a_sessao_ainda_valida(
    sessao: Session, usuario: Usuario
) -> None:
    """Metade da razão de a sessão ter estado no banco: desligar alguém derruba
    o acesso na hora, e não quando o token dele vencer."""
    agora = datetime.now(UTC)
    _, token = sessoes.criar_sessao(sessao, usuario, duracao_horas=12, agora=agora)
    assert sessoes.resolver_sessao(sessao, token, agora=agora) is not None

    usuario.ativo = False
    sessao.commit()

    assert sessoes.resolver_sessao(sessao, token, agora=agora) is None


def test_token_inexistente_devolve_none_sem_levantar(sessao: Session, usuario: Usuario) -> None:
    assert sessoes.resolver_sessao(sessao, "token-que-nunca-existiu", agora=datetime.now(UTC)) is (
        None
    )


def test_revogar_token_desconhecido_e_silencioso(sessao: Session, usuario: Usuario) -> None:
    """Logout precisa funcionar com cookie velho: quem já não tem sessão já está deslogado."""
    sessoes.revogar(sessao, "token-que-nunca-existiu", agora=datetime.now(UTC))


def test_revogar_duas_vezes_nao_muda_o_carimbo(sessao: Session, usuario: Usuario) -> None:
    agora = datetime.now(UTC)
    criada, token = sessoes.criar_sessao(sessao, usuario, duracao_horas=12, agora=agora)

    sessoes.revogar(sessao, token, agora=agora)
    primeiro = sessao.scalars(select(Sessao).where(Sessao.id == criada.id)).one().revoked_at
    sessoes.revogar(sessao, token, agora=agora + timedelta(hours=1))
    sessao.expire_all()
    segundo = sessao.scalars(select(Sessao).where(Sessao.id == criada.id)).one().revoked_at

    assert primeiro == segundo
