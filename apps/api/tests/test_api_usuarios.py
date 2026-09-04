"""Testes de integração da administração de usuários (issue #30) — contra Postgres
real (localhost:5434).

O que estes testes guardam é uma trava de segurança cada, e não "a rota
funciona": criar usuário é a operação mais perigosa da API, porque quem a tem
decide quem entra. Os blocos seguem os itens do passe de ameaça de
`auth/usuarios_router.py`:

1. quem administra (e a recusa de criar `gestor`);
2. a senha nunca passa pelo administrador — o caminho inteiro do token;
3. as três travas de auto-serviço;
4. desativar revoga sessão, e não existe `DELETE`;
5. nenhuma resposta carrega credencial, e o 409 não vira oráculo.

O banco é compartilhado com o desenvolvimento: cada teste cria usuários com
e-mail único e o teardown apaga **só** o que o teste criou — tokens de
recuperação, sessões, tentativas de login e o usuário. Nunca `TRUNCATE`, nunca
`DELETE` geral. Usuário criado pela própria API entra no `rastro` pelo id que a
resposta devolve.

O atraso progressivo do login é zerado nos overrides de `Settings`: alguns
testes erram a senha de propósito (provar que o que o administrador tem em mãos
não é senha) e a suíte não pode dormir por isso. O atraso já é provado em
`tests/test_auth_protecao.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from homecareos.auth import senhas
from homecareos.auth.dependencies import MENSAGEM_SEM_PERMISSAO
from homecareos.auth.schema import Papel
from homecareos.auth.usuarios_router import (
    MENSAGEM_AUTO_DESATIVACAO,
    MENSAGEM_EMAIL_EM_USO,
    MENSAGEM_PROPRIO_PAPEL,
    MENSAGEM_TOKEN_INDISPONIVEL,
    MENSAGEM_ULTIMO_COORDENADOR,
)
from homecareos.config import Settings, get_settings
from homecareos.db.models import Sessao, Usuario
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-usuarios"
SENHA_NOVA = "senha-nova-de-teste-usuarios"

# Campos que não podem sair em resposta nenhuma, em hipótese nenhuma.
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

    É o teardown inteiro: o banco é compartilhado, e um usuário esquecido aqui
    envenena o teste do "último coordenador ativo", que precisa contar
    coordenadores no banco todo.
    """
    ids: list[uuid.UUID] = []
    yield ids
    if ids:
        # `tentativas_login` guarda a string de e-mail tentada e não a FK, então
        # ela é apagada pela subconsulta ANTES de o usuário sumir.
        sessao.execute(
            text(
                "delete from tentativas_login where email_tentado in "
                "(select email from usuarios where id = any(:ids))"
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
    """Cria um usuário direto no banco (é assim que nasce quem já existia)."""
    usuario = Usuario(
        # O nome não carrega o papel de propósito: os testes do 409 conferem que
        # a resposta não vaza nome nem papel, e "Pessoa gestor" faria a asserção
        # do nome e a do papel se confundirem.
        nome=f"Pessoa Teste {uuid.uuid4().hex[:8]}",
        email=f"usuarios-{uuid.uuid4()}@teste.local",
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
    """Um usuário de cada papel, já no banco."""
    return {papel: _novo_usuario(sessao, rastro, papel) for papel in Papel}


def _overrides(settings: Settings, **extra: object) -> Settings:
    base: dict[str, object] = {
        "api_keys": TEST_API_KEY,
        # Fora de `local` o cookie sai `Secure`, o `TestClient` fala HTTP e o
        # cookie não seria guardado — os testes de sessão passariam a medir a
        # flag em vez da autorização.
        "environment": "local",
        "login_atraso_base_segundos": 0.0,
        "login_atraso_maximo_segundos": 0.0,
    }
    return settings.model_copy(update=base | extra)


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """Cliente sem credencial nenhuma (usado com `X-API-Key` quando preciso)."""
    app.dependency_overrides[get_settings] = lambda: _overrides(settings)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def clientes(
    settings: Settings, usuarios: dict[Papel, Usuario]
) -> Iterator[dict[Papel, TestClient]]:
    """Um `TestClient` por papel, cada um já com o cookie de sessão do seu login."""
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
        "email": f"nova-{uuid.uuid4()}@teste.local",
        "papel": papel.value,
    }


def _criar(
    cliente: TestClient, rastro: list[uuid.UUID], corpo: dict[str, str] | None = None
) -> Response:
    """Cria pela API e registra o id criado no rastro, para o teardown alcançá-lo."""
    resposta = cliente.post("/api/usuarios", json=corpo if corpo is not None else _corpo_criacao())
    if resposta.status_code == 201:
        rastro.append(uuid.UUID(resposta.json()["usuario"]["id"]))
    return resposta


def _mensagem(resposta: Response) -> str:
    corpo: dict[str, Any] = resposta.json()
    mensagem = corpo["error"]["mensagem"]
    assert isinstance(mensagem, str)
    return mensagem


# --- 1. quem administra usuário -----------------------------------------------


def test_coordenador_cria_conferente_e_coordenador(
    clientes: dict[Papel, TestClient], rastro: list[uuid.UUID]
) -> None:
    for papel in (Papel.CONFERENTE, Papel.COORDENADOR):
        corpo = _corpo_criacao(papel)
        resposta = _criar(clientes[Papel.COORDENADOR], rastro, corpo)

        assert resposta.status_code == 201, resposta.text
        criado = resposta.json()["usuario"]
        assert criado["papel"] == papel.value
        assert criado["email"] == corpo["email"]
        assert criado["ativo"] is True


def test_criar_gestor_responde_403_e_diz_o_caminho(
    clientes: dict[Papel, TestClient], rastro: list[uuid.UUID]
) -> None:
    """A trava central: `gestor` é outro eixo da matriz, não um degrau acima.

    Um coordenador que criasse um gestor se daria acesso a dado de gestão que o
    papel dele não tem — bastaria entrar na conta recém-criada.
    """
    resposta = _criar(clientes[Papel.COORDENADOR], rastro, _corpo_criacao(Papel.GESTOR))

    assert resposta.status_code == 403
    assert resposta.json()["error"]["tipo"] == "forbidden"
    mensagem = _mensagem(resposta)
    assert "gestor" in mensagem
    # E diz por onde se cria um gestor: sem isso, quem precisa de um vai
    # procurar um jeito de contornar a recusa.
    assert "homecareos.auth.cli" in mensagem


def test_promover_a_gestor_pelo_patch_tambem_responde_403(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario]
) -> None:
    """Recusar só na criação deixaria a escalada a dois passos: crie, depois promova."""
    alvo = usuarios[Papel.CONFERENTE]

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{alvo.id}", json={"papel": Papel.GESTOR.value}
    )

    assert resposta.status_code == 403
    assert "gestor" in _mensagem(resposta)


def test_conferente_e_gestor_recebem_403_nas_tres_rotas(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario]
) -> None:
    alvo = usuarios[Papel.CONFERENTE]
    for papel in (Papel.CONFERENTE, Papel.GESTOR):
        cliente = clientes[papel]

        listagem = cliente.get("/api/usuarios")
        criacao = cliente.post("/api/usuarios", json=_corpo_criacao())
        alteracao = cliente.patch(f"/api/usuarios/{alvo.id}", json={"nome": "Nome Novo"})

        assert listagem.status_code == 403, f"{papel.value} listou usuários"
        assert criacao.status_code == 403, f"{papel.value} criou usuário"
        assert alteracao.status_code == 403, f"{papel.value} alterou usuário"
        # O 403 de papel não nomeia o papel exigido — ver `exigir_papel`.
        assert _mensagem(listagem) == MENSAGEM_SEM_PERMISSAO


def test_sem_credencial_as_tres_rotas_respondem_401(
    api: TestClient, usuarios: dict[Papel, Usuario]
) -> None:
    alvo = usuarios[Papel.CONFERENTE]

    assert api.get("/api/usuarios").status_code == 401
    assert api.post("/api/usuarios", json=_corpo_criacao()).status_code == 401
    assert api.patch(f"/api/usuarios/{alvo.id}", json={"nome": "x"}).status_code == 401


# --- 2. a senha nunca passa pelo administrador --------------------------------


def test_o_token_da_criacao_define_a_senha_e_abre_o_login(
    clientes: dict[Papel, TestClient], api: TestClient, rastro: list[uuid.UUID]
) -> None:
    """O caminho inteiro, que é o que prova que a conta nasce utilizável.

    Antes de a pessoa definir a senha, **o que o administrador tem em mãos não é
    senha**: o token não abre o login. Ele só serve em `/senha/redefinir`, uma
    vez, e é aí que a conta ganha uma credencial que só a própria pessoa conhece.
    """
    corpo = _corpo_criacao()
    resposta = _criar(clientes[Papel.COORDENADOR], rastro, corpo)
    assert resposta.status_code == 201, resposta.text
    token = resposta.json()["token_definicao_senha"]
    assert token

    tentativa_com_o_token = api.post(
        "/api/auth/login", json={"email": corpo["email"], "senha": token}
    )
    assert tentativa_com_o_token.status_code == 401

    redefinicao = api.post(
        "/api/auth/senha/redefinir", json={"token": token, "nova_senha": SENHA_NOVA}
    )
    assert redefinicao.status_code == 204, redefinicao.text

    login = api.post("/api/auth/login", json={"email": corpo["email"], "senha": SENHA_NOVA})
    assert login.status_code == 200, login.text
    assert login.json()["email"] == corpo["email"]
    assert login.json()["papel"] == Papel.CONFERENTE.value

    # Uso único: o mesmo token não define a senha de novo.
    reuso = api.post(
        "/api/auth/senha/redefinir", json={"token": token, "nova_senha": SENHA_NOVA + "-2"}
    )
    assert reuso.status_code == 422


def test_a_criacao_nao_aceita_senha_no_corpo(
    clientes: dict[Papel, TestClient], rastro: list[uuid.UUID], sessao: Session
) -> None:
    """Campo extra é ignorado pelo pydantic — o que se guarda é que ele não vira credencial.

    Se um dia alguém acrescentar `senha` ao schema, este teste falha: a senha que
    o administrador mandou passaria a abrir a conta.
    """
    corpo = _corpo_criacao() | {"senha": "senha-escolhida-pelo-admin"}

    resposta = _criar(clientes[Papel.COORDENADOR], rastro, corpo)

    assert resposta.status_code == 201, resposta.text
    criado = sessao.get(Usuario, uuid.UUID(resposta.json()["usuario"]["id"]))
    assert criado is not None
    assert not senhas.verificar(criado.senha_hash, "senha-escolhida-pelo-admin")


def test_sem_token_emitido_o_usuario_nao_e_criado(
    settings: Settings, usuarios: dict[Papel, Usuario], sessao: Session
) -> None:
    """`SENHA_RESET_MAX_POR_HORA = 0` desliga a emissão: 503, e a conta é desfeita.

    `emitir_token` devolve `None` quando o teto por hora foi atingido, e deixar
    esse `None` passar em silêncio criaria uma conta sem nenhum caminho de
    primeiro acesso — ninguém perceberia até a pessoa reclamar. A conta e o token
    entram no mesmo commit, então o `rollback` desfaz a criação junto.
    """
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


# --- 3. as três travas de auto-serviço ----------------------------------------


def test_coordenador_nao_altera_o_proprio_papel(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario], sessao: Session
) -> None:
    """O próprio papel é o único cuja alteração interessa a quem ataca.

    É a trava que mantém a recusa de `gestor` valendo mesmo que a lista de papéis
    atribuíveis mude um dia, e que impede o rebaixamento acidental de quem
    administra — irreversível sem outro coordenador.
    """
    eu = usuarios[Papel.COORDENADOR]

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{eu.id}", json={"papel": Papel.CONFERENTE.value}
    )

    assert resposta.status_code == 403
    assert _mensagem(resposta) == MENSAGEM_PROPRIO_PAPEL
    sessao.expire_all()
    atual = sessao.get(Usuario, eu.id)
    assert atual is not None
    assert atual.papel == Papel.COORDENADOR.value


def test_coordenador_nao_desativa_a_si_mesmo(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario], sessao: Session
) -> None:
    eu = usuarios[Papel.COORDENADOR]

    resposta = clientes[Papel.COORDENADOR].patch(f"/api/usuarios/{eu.id}", json={"ativo": False})

    assert resposta.status_code == 403
    assert _mensagem(resposta) == MENSAGEM_AUTO_DESATIVACAO
    sessao.expire_all()
    atual = sessao.get(Usuario, eu.id)
    assert atual is not None
    assert atual.ativo is True


def test_nem_a_chave_de_api_desativa_o_ultimo_coordenador_ativo(
    api: TestClient, sessao: Session, rastro: list[uuid.UUID]
) -> None:
    """A terceira trava, pelo único caminho que a alcança.

    Com sessão de usuário esta recusa é defesa em profundidade: quem chama é
    sempre um coordenador **ativo** e não pode agir sobre a própria conta, então
    sempre resta ele. A chave de máquina passa por `exigir_papel` e não tem "si
    mesmo" — para ela, esta trava é a única, e é por isso que a verificação é
    contra o banco.
    """
    coordenador = _novo_usuario(sessao, rastro, Papel.COORDENADOR)
    outros = sessao.scalar(
        select(Usuario.id).where(
            Usuario.papel == Papel.COORDENADOR.value,
            Usuario.ativo.is_(True),
            Usuario.id != coordenador.id,
        )
    )
    assert outros is None, (
        "este teste precisa que o coordenador criado seja o último ativo do "
        "banco; há outro coordenador ativo (resíduo de teste anterior ou "
        "usuário real neste banco)"
    )

    desativacao = api.patch(
        f"/api/usuarios/{coordenador.id}", json={"ativo": False}, headers=AUTH_HEADERS
    )
    rebaixamento = api.patch(
        f"/api/usuarios/{coordenador.id}",
        json={"papel": Papel.CONFERENTE.value},
        headers=AUTH_HEADERS,
    )

    assert desativacao.status_code == 409
    assert _mensagem(desativacao) == MENSAGEM_ULTIMO_COORDENADOR
    # Rebaixar o último coordenador esvazia a coordenação do mesmo jeito que
    # desativá-lo: travar só um dos dois deixaria a porta ao lado aberta.
    assert rebaixamento.status_code == 409
    assert _mensagem(rebaixamento) == MENSAGEM_ULTIMO_COORDENADOR

    sessao.expire_all()
    atual = sessao.get(Usuario, coordenador.id)
    assert atual is not None
    assert atual.ativo is True
    assert atual.papel == Papel.COORDENADOR.value


def test_com_outro_coordenador_ativo_a_desativacao_passa(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario], rastro: list[uuid.UUID]
) -> None:
    """A trava do último coordenador não pode virar "coordenador não se desativa nunca"."""
    novo = _criar(clientes[Papel.COORDENADOR], rastro, _corpo_criacao(Papel.COORDENADOR))
    assert novo.status_code == 201, novo.text
    novo_id = novo.json()["usuario"]["id"]

    resposta = clientes[Papel.COORDENADOR].patch(f"/api/usuarios/{novo_id}", json={"ativo": False})

    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["ativo"] is False


def test_as_tres_recusas_nao_compartilham_mensagem() -> None:
    """Três travas diferentes, três mensagens — senão a tela não sabe o que dizer."""
    mensagens = {MENSAGEM_PROPRIO_PAPEL, MENSAGEM_AUTO_DESATIVACAO, MENSAGEM_ULTIMO_COORDENADOR}

    assert len(mensagens) == 3


# --- 4. desativar revoga sessão; não existe DELETE ----------------------------


def test_desativar_revoga_as_sessoes_abertas_da_pessoa(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario], sessao: Session
) -> None:
    """Sem a revogação, quem foi desligado às pressas navega por até 12h com o cookie que tem.

    A reativação é a parte que prova a revogação de verdade: `ativo = false`
    sozinho já faria o cookie devolver 401 (`sessoes.resolver_sessao` recusa
    usuário inativo), mas reativar a pessoa **ressuscitaria** aquele cookie — e
    ele pode estar num aparelho que ela não tem mais.
    """
    alvo = usuarios[Papel.CONFERENTE]
    cliente_alvo = clientes[Papel.CONFERENTE]
    assert cliente_alvo.get("/api/auth/eu").status_code == 200

    desativacao = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{alvo.id}", json={"ativo": False}
    )

    assert desativacao.status_code == 200, desativacao.text
    assert desativacao.json()["ativo"] is False
    assert cliente_alvo.get("/api/auth/eu").status_code == 401

    sessao.expire_all()
    abertas = sessao.scalars(
        select(Sessao).where(Sessao.usuario_id == alvo.id, Sessao.revoked_at.is_(None))
    ).all()
    assert not abertas

    reativacao = clientes[Papel.COORDENADOR].patch(f"/api/usuarios/{alvo.id}", json={"ativo": True})
    assert reativacao.status_code == 200, reativacao.text
    assert cliente_alvo.get("/api/auth/eu").status_code == 401


def test_nao_existe_delete_de_usuario(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario]
) -> None:
    """`log_conferencia.usuario_id` aponta para `usuarios`: apagar alguém apagaria
    a resposta a "quem fez esta ação?", que é a razão de existir da issue #30."""
    alvo = usuarios[Papel.CONFERENTE]

    resposta = clientes[Papel.COORDENADOR].delete(f"/api/usuarios/{alvo.id}")

    assert resposta.status_code == 405


def test_patch_de_usuario_inexistente_responde_404(clientes: dict[Papel, TestClient]) -> None:
    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/usuarios/{uuid.uuid4()}", json={"nome": "Ninguém"}
    )

    assert resposta.status_code == 404


# --- 5. nada de credencial na resposta, e o 409 não vira oráculo --------------


def test_nenhuma_resposta_carrega_senha_hash_mfa_secret_nem_ultimo_passo(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
    rastro: list[uuid.UUID],
) -> None:
    """As quatro respostas que mostram um usuário, conferidas no texto cru.

    Além do nome dos campos, o **valor** do hash é procurado no corpo: um schema
    que passasse a serializar o model inteiro seria pego pelas duas pontas.
    """
    coordenador = clientes[Papel.COORDENADOR]
    criacao = _criar(coordenador, rastro)
    assert criacao.status_code == 201, criacao.text
    criado_id = uuid.UUID(criacao.json()["usuario"]["id"])
    listagem = coordenador.get("/api/usuarios")
    alteracao = coordenador.patch(f"/api/usuarios/{criado_id}", json={"nome": "Nome Alterado"})

    sessao.expire_all()
    criado = sessao.get(Usuario, criado_id)
    assert criado is not None
    hashes = {criado.senha_hash, usuarios[Papel.CONFERENTE].senha_hash}

    for resposta in (criacao, listagem, alteracao):
        assert resposta.status_code in (200, 201), resposta.text
        texto = resposta.text
        for campo in CAMPOS_PROIBIDOS:
            assert campo not in texto, f"{campo} vazou em {resposta.request.url}"
        for hash_de_senha in hashes:
            assert hash_de_senha not in texto


def test_email_duplicado_responde_409_sem_revelar_o_usuario_existente(
    clientes: dict[Papel, TestClient], usuarios: dict[Papel, Usuario], rastro: list[uuid.UUID]
) -> None:
    """O e-mail em maiúsculas prova a normalização, e a mensagem neutra fecha o oráculo.

    Quem tiver uma sessão de coordenador comprometida não pode descobrir, pela
    resposta, o nome e o papel de quem já está cadastrado.
    """
    existente = usuarios[Papel.GESTOR]
    corpo = _corpo_criacao() | {"email": existente.email.upper()}

    resposta = _criar(clientes[Papel.COORDENADOR], rastro, corpo)

    assert resposta.status_code == 409
    assert resposta.json()["error"]["tipo"] == "conflict"
    assert _mensagem(resposta) == MENSAGEM_EMAIL_EM_USO
    texto = resposta.text.lower()
    assert existente.nome.lower() not in texto
    for papel in Papel:
        assert papel.value not in texto


# --- a listagem ---------------------------------------------------------------


def test_listagem_filtra_por_ativo_e_pagina(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
    rastro: list[uuid.UUID],
) -> None:
    desativado = _novo_usuario(sessao, rastro, Papel.CONFERENTE, ativo=False)
    coordenador = clientes[Papel.COORDENADOR]

    inativos = coordenador.get("/api/usuarios?ativo=false")
    ativos = coordenador.get("/api/usuarios?ativo=true&limite=200")
    primeira_pagina = coordenador.get("/api/usuarios?limite=1")

    assert inativos.status_code == 200
    emails_inativos = [item["email"] for item in inativos.json()["data"]]
    assert desativado.email in emails_inativos
    assert usuarios[Papel.CONFERENTE].email not in emails_inativos

    assert ativos.status_code == 200
    emails_ativos = [item["email"] for item in ativos.json()["data"]]
    assert usuarios[Papel.CONFERENTE].email in emails_ativos
    assert desativado.email not in emails_ativos

    assert primeira_pagina.status_code == 200
    corpo = primeira_pagina.json()
    assert len(corpo["data"]) == 1
    assert corpo["paginacao"]["limite"] == 1
    assert corpo["paginacao"]["offset"] == 0
    # Os três papéis da fixture mais o desativado deste teste.
    assert corpo["paginacao"]["total"] >= 4
