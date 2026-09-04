"""Testes de integração da auditoria administrativa de usuários (issue #30) — contra
Postgres real (localhost:5434).

O que estes testes guardam é o requisito duro do handoff: o registro de
auditoria entra na **mesma transação** da mutação que o originou, nunca a
mais nem a menos. Os blocos seguem os critérios de aceite do handoff:

1. criação grava exatamente um registro, com ator, alvo, ação e o papel
   atribuído;
2. alteração grava valor anterior e novo de cada campo que mudou;
3. dois campos numa só chamada são auditados sem perder nenhum;
4. `PATCH` que não muda nada de fato não grava registro vazio;
5. 503 por teto de token não deixa registro órfão — o rollback leva os dois;
6. cada recusa (403 de gestor, 403 de auto-serviço, 409 de último coordenador,
   404) não gera registro: nada mudou;
7. `X-API-Key` é auditada com ator nulo e rótulo `"api"`;
8. `GET` é paginado, ordenado do mais recente para o mais antigo com
   desempate, filtrável por alvo, e recusa quem não é coordenador;
9. nenhuma credencial aparece na tabela nem na resposta.

Fixtures e convenções copiadas de `test_api_usuarios.py`: banco compartilhado
com o desenvolvimento, cada teste cria usuário com e-mail único, e o teardown
apaga **só** o que o teste criou. Nunca `TRUNCATE`, nunca `DELETE` geral.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from homecareos.auth import senhas
from homecareos.auth.dependencies import MENSAGEM_SEM_PERMISSAO
from homecareos.auth.schema import AcaoAuditoriaUsuario, Papel
from homecareos.auth.usuarios_router import (
    MENSAGEM_AUTO_DESATIVACAO,
    MENSAGEM_PROPRIO_PAPEL,
    MENSAGEM_TOKEN_INDISPONIVEL,
    MENSAGEM_ULTIMO_COORDENADOR,
)
from homecareos.config import Settings, get_settings
from homecareos.db.models import AuditoriaUsuario, Usuario
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-auditoria"

# Campos que não podem sair em resposta nenhuma, em hipótese nenhuma — mesma
# constante de `test_api_usuarios.py`.
CAMPOS_PROIBIDOS = ("senha_hash", "mfa_secret", "mfa_ultimo_passo")


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
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def rastro(sessao: Session) -> Iterator[list[uuid.UUID]]:
    """Ids dos usuários que este teste criou — inclusive os criados pela API.

    Mesmo teardown de `test_api_usuarios.py`, incluindo `auditoria_usuarios`
    antes de `usuarios`: a tabela nova referencia `usuarios` como ator e como
    alvo, e sem apagá-la primeiro a FK bloqueia o `delete` de `usuarios`.
    """
    ids: list[uuid.UUID] = []
    yield ids
    if ids:
        sessao.execute(
            text(
                "delete from tentativas_login where email_tentado in "
                "(select email from usuarios where id = any(:ids))"
            ),
            {"ids": ids},
        )
        sessao.execute(
            text(
                "delete from auditoria_usuarios where alvo_usuario_id = any(:ids) "
                "or usuario_id = any(:ids)"
            ),
            {"ids": ids},
        )
        sessao.execute(
            text("delete from tokens_recuperacao where usuario_id = any(:ids)"), {"ids": ids}
        )
        sessao.execute(text("delete from sessoes where usuario_id = any(:ids)"), {"ids": ids})
        sessao.execute(text("delete from usuarios where id = any(:ids)"), {"ids": ids})
        sessao.commit()


def _novo_usuario(
    sessao: Session, rastro: list[uuid.UUID], papel: Papel, *, ativo: bool = True
) -> Usuario:
    usuario = Usuario(
        nome=f"Pessoa Teste {uuid.uuid4().hex[:8]}",
        email=f"auditoria-{uuid.uuid4()}@teste.local",
        senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
        papel=papel.value,
        ativo=ativo,
    )
    sessao.add(usuario)
    sessao.commit()
    rastro.append(usuario.id)
    return usuario


@pytest.fixture
def usuarios(sessao: Session, rastro: list[uuid.UUID]) -> dict[Papel, Usuario]:
    return {papel: _novo_usuario(sessao, rastro, papel) for papel in Papel}


def _overrides(settings: Settings, **extra: object) -> Settings:
    base: dict[str, object] = {
        "api_keys": TEST_API_KEY,
        "environment": "local",
        "login_atraso_base_segundos": 0.0,
        "login_atraso_maximo_segundos": 0.0,
    }
    return settings.model_copy(update=base | extra)


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: _overrides(settings)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def clientes(
    settings: Settings, usuarios: dict[Papel, Usuario]
) -> Iterator[dict[Papel, TestClient]]:
    app.dependency_overrides[get_settings] = lambda: _overrides(settings)
    try:
        logados = {}
        for papel, usuario in usuarios.items():
            cliente = TestClient(app)
            resposta = cliente.post(
                "/api/auth/login", json={"email": usuario.email, "senha": SENHA_DE_TESTE}
            )
            assert resposta.status_code == 200, resposta.text
            logados[papel] = cliente
        yield logados
    finally:
        app.dependency_overrides.clear()


def _corpo_criacao(papel: Papel = Papel.CONFERENTE) -> dict[str, str]:
    return {
        "nome": f"Pessoa Nova {uuid.uuid4().hex[:8]}",
        "email": f"auditoria-nova-{uuid.uuid4()}@teste.local",
        "papel": papel.value,
    }


def _criar(
    cliente: TestClient, rastro: list[uuid.UUID], corpo: dict[str, str] | None = None
) -> Response:
    resposta = cliente.post("/api/usuarios", json=corpo if corpo is not None else _corpo_criacao())
    if resposta.status_code == 201:
        rastro.append(uuid.UUID(resposta.json()["usuario"]["id"]))
    return resposta


def _mensagem(resposta: Response) -> str:
    corpo: dict[str, Any] = resposta.json()
    mensagem = corpo["error"]["mensagem"]
    assert isinstance(mensagem, str)
    return mensagem


def _eventos_do_alvo(sessao: Session, alvo_id: uuid.UUID) -> list[AuditoriaUsuario]:
    sessao.expire_all()
    return list(
        sessao.scalars(
            select(AuditoriaUsuario)
            .where(AuditoriaUsuario.alvo_usuario_id == alvo_id)
            .order_by(AuditoriaUsuario.created_at)
        )
    )


# --- 1. criação: um registro, com ator, alvo, ação e papel ---------------------


def test_criar_usuario_grava_um_registro_com_ator_alvo_acao_e_papel(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
    rastro: list[uuid.UUID],
) -> None:
    coordenador = usuarios[Papel.COORDENADOR]
    corpo = _corpo_criacao(Papel.CONFERENTE)

    resposta = _criar(clientes[Papel.COORDENADOR], rastro, corpo)
    assert resposta.status_code == 201, resposta.text
    criado_id = uuid.UUID(resposta.json()["usuario"]["id"])

    eventos = _eventos_do_alvo(sessao, criado_id)
    assert len(eventos) == 1
    evento = eventos[0]
    assert evento.acao == AcaoAuditoriaUsuario.CRIACAO.value
    assert evento.usuario == coordenador.email
    assert evento.usuario_id == coordenador.id
    assert evento.alvo_usuario_id == criado_id
    assert evento.alvo_email == corpo["email"].lower()
    assert evento.mudancas["papel"] == {"de": None, "para": Papel.CONFERENTE.value}


# --- 2. alteração: valor anterior e novo ---------------------------------------


def test_alterar_nome_grava_valor_anterior_e_novo(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
) -> None:
    alvo = usuarios[Papel.CONFERENTE]
    nome_original = alvo.nome

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{alvo.id}", json={"nome": "Novo Nome de Teste"}
    )
    assert resposta.status_code == 200, resposta.text

    eventos = _eventos_do_alvo(sessao, alvo.id)
    assert len(eventos) == 1
    assert eventos[0].acao == AcaoAuditoriaUsuario.ALTERACAO.value
    assert eventos[0].mudancas == {"nome": {"de": nome_original, "para": "Novo Nome de Teste"}}


def test_desativar_e_reativar_gravam_a_acao_correspondente(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
) -> None:
    alvo = usuarios[Papel.CONFERENTE]

    desativacao = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{alvo.id}", json={"ativo": False}
    )
    reativacao = clientes[Papel.COORDENADOR].patch(f"/api/usuarios/{alvo.id}", json={"ativo": True})
    assert desativacao.status_code == 200, desativacao.text
    assert reativacao.status_code == 200, reativacao.text

    eventos = _eventos_do_alvo(sessao, alvo.id)
    assert [e.acao for e in eventos] == [
        AcaoAuditoriaUsuario.DESATIVACAO.value,
        AcaoAuditoriaUsuario.REATIVACAO.value,
    ]
    assert eventos[0].mudancas == {"ativo": {"de": True, "para": False}}
    assert eventos[1].mudancas == {"ativo": {"de": False, "para": True}}


# --- 3. dois campos numa só chamada: nenhum se perde ---------------------------


def test_alterar_dois_campos_em_uma_chamada_audita_os_dois(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
) -> None:
    alvo = usuarios[Papel.CONFERENTE]
    nome_original = alvo.nome

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{alvo.id}",
        json={"nome": "Pessoa Promovida", "papel": Papel.COORDENADOR.value},
    )
    assert resposta.status_code == 200, resposta.text

    eventos = _eventos_do_alvo(sessao, alvo.id)
    assert len(eventos) == 1
    # Ativo não mudou nesta chamada, então a ação é a genérica — mas os dois
    # campos que mudaram entram no diff, sem perder nenhum.
    assert eventos[0].acao == AcaoAuditoriaUsuario.ALTERACAO.value
    assert eventos[0].mudancas == {
        "nome": {"de": nome_original, "para": "Pessoa Promovida"},
        "papel": {"de": Papel.CONFERENTE.value, "para": Papel.COORDENADOR.value},
    }


# --- 4. PATCH que não muda nada não grava registro vazio -----------------------


def test_patch_que_nao_muda_nada_de_fato_nao_grava_registro(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
) -> None:
    """Reenviar o valor que já está no banco não é mudança, mesmo que o campo venha no corpo."""
    alvo = usuarios[Papel.CONFERENTE]

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{alvo.id}",
        json={"nome": alvo.nome, "papel": alvo.papel, "ativo": alvo.ativo},
    )
    assert resposta.status_code == 200, resposta.text

    assert _eventos_do_alvo(sessao, alvo.id) == []


# --- 5. 503 no POST: rollback leva os dois juntos -------------------------------


def test_503_por_teto_de_token_nao_deixa_registro_de_auditoria_orfao(
    settings: Settings,
    usuarios: dict[Papel, Usuario],
    sessao: Session,
) -> None:
    app.dependency_overrides[get_settings] = lambda: _overrides(
        settings, senha_reset_max_por_hora=0
    )
    corpo = _corpo_criacao()
    try:
        cliente = TestClient(app)
        login = cliente.post(
            "/api/auth/login",
            json={"email": usuarios[Papel.COORDENADOR].email, "senha": SENHA_DE_TESTE},
        )
        assert login.status_code == 200, login.text
        resposta = cliente.post("/api/usuarios", json=corpo)
    finally:
        app.dependency_overrides.clear()

    assert resposta.status_code == 503
    assert _mensagem(resposta) == MENSAGEM_TOKEN_INDISPONIVEL

    sessao.expire_all()
    assert sessao.scalar(select(Usuario.id).where(Usuario.email == corpo["email"])) is None
    orfao = sessao.scalar(
        select(AuditoriaUsuario.id).where(AuditoriaUsuario.alvo_email == corpo["email"])
    )
    assert orfao is None


# --- 6. recusas não geram auditoria: nada mudou ---------------------------------


def test_criar_gestor_recusado_nao_gera_auditoria(
    clientes: dict[Papel, TestClient], sessao: Session
) -> None:
    """Recusado antes de qualquer `Usuario` existir: não há alvo para filtrar.

    A tabela inteira não pode ganhar linha nesta chamada — o `count()` antes e
    depois é a asserção, já que nenhuma criação bem-sucedida acontece no teste.
    """
    antes = sessao.scalar(select(func.count()).select_from(AuditoriaUsuario))

    resposta = clientes[Papel.COORDENADOR].post("/api/usuarios", json=_corpo_criacao(Papel.GESTOR))
    assert resposta.status_code == 403

    sessao.expire_all()
    depois = sessao.scalar(select(func.count()).select_from(AuditoriaUsuario))
    assert depois == antes


def test_promover_a_gestor_recusado_nao_gera_auditoria(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario], sessao: Session
) -> None:
    alvo = usuarios[Papel.CONFERENTE]

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{alvo.id}", json={"papel": Papel.GESTOR.value}
    )

    assert resposta.status_code == 403
    assert _eventos_do_alvo(sessao, alvo.id) == []


def test_proprio_papel_recusado_nao_gera_auditoria(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario], sessao: Session
) -> None:
    eu = usuarios[Papel.COORDENADOR]

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{eu.id}", json={"papel": Papel.CONFERENTE.value}
    )

    assert resposta.status_code == 403
    assert _mensagem(resposta) == MENSAGEM_PROPRIO_PAPEL
    assert _eventos_do_alvo(sessao, eu.id) == []


def test_auto_desativacao_recusada_nao_gera_auditoria(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario], sessao: Session
) -> None:
    eu = usuarios[Papel.COORDENADOR]

    resposta = clientes[Papel.COORDENADOR].patch(f"/api/usuarios/{eu.id}", json={"ativo": False})

    assert resposta.status_code == 403
    assert _mensagem(resposta) == MENSAGEM_AUTO_DESATIVACAO
    assert _eventos_do_alvo(sessao, eu.id) == []


def test_ultimo_coordenador_recusado_nao_gera_auditoria(
    api: TestClient, sessao: Session, rastro: list[uuid.UUID]
) -> None:
    coordenador = _novo_usuario(sessao, rastro, Papel.COORDENADOR)
    outros = sessao.scalar(
        select(Usuario.id).where(
            Usuario.papel == Papel.COORDENADOR.value,
            Usuario.ativo.is_(True),
            Usuario.id != coordenador.id,
        )
    )
    assert outros is None, (
        "este teste precisa que o coordenador criado seja o último ativo do banco"
    )

    resposta = api.patch(
        f"/api/usuarios/{coordenador.id}", json={"ativo": False}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 409
    assert _mensagem(resposta) == MENSAGEM_ULTIMO_COORDENADOR
    assert _eventos_do_alvo(sessao, coordenador.id) == []


def test_patch_de_inexistente_404_nao_gera_auditoria(
    clientes: dict[Papel, TestClient], sessao: Session
) -> None:
    alvo_id = uuid.uuid4()

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{alvo_id}", json={"nome": "Ninguém"}
    )

    assert resposta.status_code == 404
    assert _eventos_do_alvo(sessao, alvo_id) == []


# --- 7. X-API-Key: ator nulo, rótulo "api" --------------------------------------


def test_chamada_por_api_key_e_auditada_com_ator_nulo_e_rotulo_api(
    api: TestClient, sessao: Session, rastro: list[uuid.UUID]
) -> None:
    corpo = _corpo_criacao()

    resposta = api.post("/api/usuarios", json=corpo, headers=AUTH_HEADERS)
    assert resposta.status_code == 201, resposta.text
    criado_id = uuid.UUID(resposta.json()["usuario"]["id"])
    rastro.append(criado_id)

    eventos = _eventos_do_alvo(sessao, criado_id)
    assert len(eventos) == 1
    assert eventos[0].usuario_id is None
    assert eventos[0].usuario == "api"


# --- 8. GET: paginação, ordenação, desempate, filtro, autorização --------------


def test_get_auditoria_pagina_ordena_do_mais_recente_e_filtra_por_alvo(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
) -> None:
    alvo = usuarios[Papel.CONFERENTE]
    coordenador = clientes[Papel.COORDENADOR]

    for nome in ("Primeiro Nome", "Segundo Nome", "Terceiro Nome"):
        resposta = coordenador.patch(f"/api/usuarios/{alvo.id}", json={"nome": nome})
        assert resposta.status_code == 200, resposta.text

    listagem = coordenador.get(f"/api/usuarios/auditoria?alvo_id={alvo.id}&limite=200")
    assert listagem.status_code == 200, listagem.text
    corpo = listagem.json()
    itens = corpo["data"]
    assert len(itens) == 3
    assert all(item["alvo_usuario_id"] == str(alvo.id) for item in itens)
    # Mais recente primeiro: o último `PATCH` (Terceiro Nome) aparece primeiro.
    assert itens[0]["mudancas"]["nome"]["para"] == "Terceiro Nome"
    assert itens[-1]["mudancas"]["nome"]["para"] == "Primeiro Nome"

    primeira_pagina = coordenador.get(
        f"/api/usuarios/auditoria?alvo_id={alvo.id}&limite=1&offset=0"
    )
    segunda_pagina = coordenador.get(f"/api/usuarios/auditoria?alvo_id={alvo.id}&limite=1&offset=1")
    assert primeira_pagina.status_code == 200
    assert segunda_pagina.status_code == 200
    assert primeira_pagina.json()["data"][0]["id"] != segunda_pagina.json()["data"][0]["id"]
    assert primeira_pagina.json()["paginacao"]["total"] == 3


def test_get_auditoria_recusa_quem_nao_e_coordenador(
    clientes: dict[Papel, TestClient],
) -> None:
    for papel in (Papel.CONFERENTE, Papel.GESTOR):
        resposta = clientes[papel].get("/api/usuarios/auditoria")
        assert resposta.status_code == 403
        assert _mensagem(resposta) == MENSAGEM_SEM_PERMISSAO


def test_get_auditoria_sem_credencial_responde_401() -> None:
    cliente = TestClient(app)
    resposta = cliente.get("/api/usuarios/auditoria")
    assert resposta.status_code == 401


# --- 9. nenhuma credencial na tabela nem na resposta ----------------------------


def test_resposta_e_registro_nunca_carregam_credencial(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
    rastro: list[uuid.UUID],
) -> None:
    coordenador = clientes[Papel.COORDENADOR]
    criacao = _criar(coordenador, rastro)
    assert criacao.status_code == 201, criacao.text
    criado_id = uuid.UUID(criacao.json()["usuario"]["id"])
    coordenador.patch(f"/api/usuarios/{criado_id}", json={"nome": "Outro Nome"})

    listagem = coordenador.get(f"/api/usuarios/auditoria?alvo_id={criado_id}")
    assert listagem.status_code == 200, listagem.text

    sessao.expire_all()
    hashes = {u.senha_hash for u in usuarios.values()}
    for campo in CAMPOS_PROIBIDOS:
        assert campo not in listagem.text, f"{campo} vazou em /api/usuarios/auditoria"
    for hash_de_senha in hashes:
        assert hash_de_senha not in listagem.text

    eventos = _eventos_do_alvo(sessao, criado_id)
    for evento in eventos:
        for chave in evento.mudancas:
            assert chave in {"nome", "papel", "ativo"}
