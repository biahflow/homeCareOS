"""Testes de integração de `/api/auth/*` — contra Postgres real (localhost:5434).

O banco é compartilhado com o desenvolvimento: cada fixture cria usuário com
e-mail único e apaga sessões e usuário no fim.

**Nenhum teste imprime senha**, e um deles afirma explicitamente que a senha não
aparece no corpo bruto de resposta nenhuma — é o critério de aceite nº 2 da
issue #30.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from homecareos.auth import senhas, sessoes
from homecareos.auth.schema import Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import Usuario
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-api-auth"


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
    """Cliente contra a app real.

    `environment="local"` é fixado de propósito: fora de `local` o cookie de
    sessão sai com a flag `Secure`, e o `TestClient` fala HTTP — o cookie não
    seria guardado e os testes de sessão passariam a medir a flag em vez do
    login. É a mesma razão pela qual a flag é condicional no código.
    """
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"api_keys": TEST_API_KEY, "environment": "local"}
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def _criar_usuario(
    session: Session, *, papel: Papel = Papel.CONFERENTE, ativo: bool = True
) -> Usuario:
    usuario = Usuario(
        nome="Pessoa de Teste",
        email=f"api-auth-{uuid.uuid4()}@teste.local",
        senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
        papel=papel.value,
        ativo=ativo,
    )
    session.add(usuario)
    session.commit()
    return usuario


def _limpar_usuarios(session: Session, ids: list[uuid.UUID]) -> None:
    session.execute(text("delete from sessoes where usuario_id = any(:ids)"), {"ids": ids})
    session.execute(text("delete from usuarios where id = any(:ids)"), {"ids": ids})
    session.commit()


@pytest.fixture
def usuario(sessao: Session) -> Iterator[Usuario]:
    linha = _criar_usuario(sessao)
    yield linha
    _limpar_usuarios(sessao, [linha.id])


@pytest.fixture
def usuario_inativo(sessao: Session) -> Iterator[Usuario]:
    linha = _criar_usuario(sessao, ativo=False)
    yield linha
    _limpar_usuarios(sessao, [linha.id])


def _login(api: TestClient, usuario: Usuario, senha: str = SENHA_DE_TESTE):  # type: ignore[no-untyped-def]
    return api.post("/api/auth/login", json={"email": usuario.email, "senha": senha})


# --- login --------------------------------------------------------------------


def test_login_com_credencial_correta_responde_200_e_seta_cookie_httponly(
    api: TestClient, usuario: Usuario
) -> None:
    resposta = _login(api, usuario)

    assert resposta.status_code == 200
    assert resposta.json()["email"] == usuario.email
    assert resposta.json()["papel"] == "conferente"

    (set_cookie,) = [
        valor for chave, valor in resposta.headers.items() if chave.lower() == "set-cookie"
    ]
    assert "homecareos_sessao=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()


def test_login_com_senha_errada_e_email_inexistente_respondem_o_mesmo_corpo(
    api: TestClient, usuario: Usuario
) -> None:
    """Os dois corpos são comparados **entre si**, e não com um literal: o que
    precisa valer é a indistinguibilidade, não o texto de hoje."""
    senha_errada = _login(api, usuario, senha="nao-e-a-senha")
    email_inexistente = api.post(
        "/api/auth/login",
        json={"email": f"ninguem-{uuid.uuid4()}@teste.local", "senha": SENHA_DE_TESTE},
    )

    assert senha_errada.status_code == email_inexistente.status_code == 401
    assert senha_errada.json() == email_inexistente.json()


def test_login_de_usuario_inativo_responde_o_mesmo_401(
    api: TestClient, usuario: Usuario, usuario_inativo: Usuario
) -> None:
    inativo = _login(api, usuario_inativo)
    senha_errada = _login(api, usuario, senha="nao-e-a-senha")

    assert inativo.status_code == senha_errada.status_code == 401
    assert inativo.json() == senha_errada.json()


def test_login_normaliza_o_email_para_minusculas(api: TestClient, usuario: Usuario) -> None:
    resposta = api.post(
        "/api/auth/login", json={"email": usuario.email.upper(), "senha": SENHA_DE_TESTE}
    )

    assert resposta.status_code == 200


def test_a_senha_nunca_aparece_no_corpo_de_resposta_nenhuma(
    api: TestClient, usuario: Usuario
) -> None:
    """Critério de aceite nº 2: senha nunca trafega em claro na resposta."""
    login = _login(api, usuario)
    eu = api.get("/api/auth/eu")

    for resposta in (login, eu):
        assert resposta.status_code == 200
        assert SENHA_DE_TESTE not in resposta.text
        assert "senha" not in resposta.text.lower()


def test_o_hash_da_senha_nunca_aparece_no_corpo_de_resposta_nenhuma(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    login = _login(api, usuario)

    sessao.refresh(usuario)
    assert usuario.senha_hash not in login.text


# --- cookie de sessão como credencial ----------------------------------------


def test_cookie_de_login_da_acesso_a_rota_protegida_sem_x_api_key(
    api: TestClient, usuario: Usuario
) -> None:
    assert api.get("/api/operadoras").status_code == 401

    assert _login(api, usuario).status_code == 200

    resposta = api.get("/api/operadoras")
    assert resposta.status_code == 200
    assert "X-API-Key" not in resposta.request.headers


def test_logout_revoga_a_sessao_e_a_mesma_requisicao_passa_a_responder_401(
    api: TestClient, usuario: Usuario
) -> None:
    _login(api, usuario)
    assert api.get("/api/operadoras").status_code == 200

    cookie = api.cookies.get("homecareos_sessao")
    assert cookie is not None
    assert api.post("/api/auth/logout").status_code == 204

    # O cookie antigo, reapresentado à mão, não vale mais: a revogação é de
    # servidor, não de navegador. O jar é limpo antes para o cookie do
    # argumento ser o único que viaja.
    api.cookies.clear()
    assert api.get("/api/operadoras", cookies={"homecareos_sessao": cookie}).status_code == 401


def test_logout_sem_cookie_responde_204_do_mesmo_jeito(api: TestClient) -> None:
    assert api.post("/api/auth/logout").status_code == 204


def test_sessao_expirada_responde_401_como_credencial_ausente(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    _, token = sessoes.criar_sessao(
        sessao, usuario, duracao_horas=12, agora=datetime.now(UTC) - timedelta(hours=13)
    )

    expirada = api.get("/api/operadoras", cookies={"homecareos_sessao": token})
    sem_credencial = api.get("/api/operadoras")

    assert expirada.status_code == sem_credencial.status_code == 401
    assert expirada.json() == sem_credencial.json()


def test_usuario_desativado_perde_o_acesso_na_requisicao_seguinte(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    _login(api, usuario)
    assert api.get("/api/operadoras").status_code == 200

    usuario.ativo = False
    sessao.commit()

    assert api.get("/api/operadoras").status_code == 401


def test_login_que_falha_nao_derruba_a_sessao_de_quem_ja_estava_dentro(
    api: TestClient, usuario: Usuario
) -> None:
    """Regressão: a revogação da sessão anterior só pode ocorrer APÓS aceitar a credencial.

    Revogando na entrada, uma senha digitada errada deslogava quem já estava
    autenticado no mesmo navegador — comportamento que ninguém espera, e um
    incômodo autoinfligido dentro da própria origem (`samesite="lax"` impede que
    um site de terceiros dispare o POST com o cookie da vítima).
    """
    _login(api, usuario)
    assert api.get("/api/auth/eu").status_code == 200

    falha = api.post(
        "/api/auth/login", json={"email": usuario.email, "senha": "senha-digitada-errada"}
    )

    assert falha.status_code == 401
    assert api.get("/api/auth/eu").status_code == 200


def test_relogin_revoga_a_sessao_anterior(api: TestClient, usuario: Usuario) -> None:
    """Sem isto, cada relogin deixaria uma sessão órfã válida até expirar."""
    _login(api, usuario)
    cookie_antigo = api.cookies.get("homecareos_sessao")
    assert cookie_antigo is not None

    _login(api, usuario)
    api.cookies.clear()

    assert api.get("/api/operadoras", cookies={"homecareos_sessao": cookie_antigo}).status_code == (
        401
    )


# --- GET /api/auth/eu ---------------------------------------------------------


def test_eu_com_sessao_devolve_o_usuario(api: TestClient, usuario: Usuario) -> None:
    _login(api, usuario)

    resposta = api.get("/api/auth/eu")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["id"] == str(usuario.id)
    assert corpo["email"] == usuario.email


def test_eu_com_x_api_key_devolve_maquina_e_nao_um_usuario_forjado(api: TestClient) -> None:
    resposta = api.get("/api/auth/eu", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    assert resposta.json() == {"tipo": "maquina"}


def test_eu_sem_credencial_responde_401(api: TestClient) -> None:
    assert api.get("/api/auth/eu").status_code == 401


def test_sessao_tem_precedencia_sobre_a_chave_de_api(api: TestClient, usuario: Usuario) -> None:
    """Quem manda as duas credenciais é tratado como a pessoa.

    Invertido, a auditoria perderia justamente a identidade que a issue #30
    existe para registrar.
    """
    _login(api, usuario)

    resposta = api.get("/api/auth/eu", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    assert resposta.json()["email"] == usuario.email
