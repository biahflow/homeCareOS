"""Testes de integração da recuperação de senha (issue #34) — contra Postgres
real (localhost:5434).

**Nenhuma conexão SMTP.** O gateway de e-mail é um dublê em memória
(`ProviderFalso`) injetado por `app.dependency_overrides[provider_de_email]`;
o contrato do envio de verdade é exercitado, sem rede, em
`tests/test_mailer_smtp.py`.

O banco é compartilhado com o desenvolvimento: cada teste cria usuário com
e-mail único e o teardown apaga **só** o que o teste criou — tokens de
recuperação, sessões, tentativas de login e o usuário. Nunca `TRUNCATE`, nunca
`DELETE` geral.

O atraso progressivo do login é zerado nos overrides de `Settings`: alguns
testes precisam errar a senha de propósito (provar que a antiga deixou de
valer) e a suíte não pode dormir por isso. O atraso já é provado em
`tests/test_auth_protecao.py`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from homecareos.auth import recuperacao, senhas
from homecareos.auth.router import (
    ASSUNTO_RECUPERACAO,
    MENSAGEM_TOKEN_INVALIDO,
    provider_de_email,
)
from homecareos.auth.schema import Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import TokenRecuperacao, Usuario
from homecareos.db.session import get_sessionmaker
from homecareos.mailer.errors import EnvioEmailError
from homecareos.main import app
from tests.conftest import TEST_API_KEY, TEST_API_KEY_PAPEIS

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-recuperacao"
SENHA_NOVA = "senha-nova-de-teste-recuperacao"
# Abaixo de `senha_minima_caracteres` (12) de propósito — ver o caso 12.
SENHA_CURTA = "curta"


class ProviderFalso:
    """Gateway de e-mail em memória. Acumula `(destinatario, assunto, corpo)`.

    `erro` faz o próximo envio levantar `EnvioEmailError`, que é como o caso 6
    prova que SMTP fora do ar não vira 500 (e não volta a dizer quem está
    cadastrado).
    """

    def __init__(self, erro: Exception | None = None) -> None:
        self.erro = erro
        self.enviados: list[tuple[str, str, str]] = []

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        if self.erro is not None:
            raise self.erro
        self.enviados.append((destinatario, assunto, corpo))


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


@contextmanager
def _cliente(
    settings: Settings, provider: ProviderFalso | None, **extra: object
) -> Iterator[TestClient]:
    """Cliente contra a app real, com o gateway de e-mail substituído.

    `environment="local"` é fixado de propósito: fora de `local` o cookie de
    sessão sai com `Secure`, o `TestClient` fala HTTP e o cookie não seria
    guardado — o caso 8, que prova a revogação das sessões, passaria a medir a
    flag em vez da revogação.
    """
    base: dict[str, object] = {
        "api_keys": TEST_API_KEY,
        "api_key_papeis": TEST_API_KEY_PAPEIS,
        "environment": "local",
        "login_atraso_base_segundos": 0.0,
        "login_atraso_maximo_segundos": 0.0,
    }
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update=base | extra)
    app.dependency_overrides[provider_de_email] = lambda: provider
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def provider() -> ProviderFalso:
    return ProviderFalso()


@pytest.fixture
def api(settings: Settings, provider: ProviderFalso) -> Iterator[TestClient]:
    with _cliente(settings, provider) as cliente:
        yield cliente


@pytest.fixture
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def _criar_usuario(session: Session, *, ativo: bool = True) -> Usuario:
    usuario = Usuario(
        nome="Pessoa de Teste - Recuperação",
        email=f"recuperacao-{uuid.uuid4()}@teste.local",
        senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
        papel=Papel.CONFERENTE.value,
        ativo=ativo,
    )
    session.add(usuario)
    session.commit()
    return usuario


def _limpar_usuario(session: Session, usuario: Usuario) -> None:
    """Apaga só o rastro deste usuário — o banco é compartilhado."""
    session.execute(
        text("delete from tokens_recuperacao where usuario_id = :id"), {"id": usuario.id}
    )
    session.execute(text("delete from sessoes where usuario_id = :id"), {"id": usuario.id})
    session.execute(
        text("delete from tentativas_login where email_tentado = :email"),
        {"email": usuario.email},
    )
    session.execute(text("delete from usuarios where id = :id"), {"id": usuario.id})
    session.commit()


@pytest.fixture
def usuario(sessao: Session) -> Iterator[Usuario]:
    linha = _criar_usuario(sessao)
    yield linha
    _limpar_usuario(sessao, linha)


@pytest.fixture
def usuario_inativo(sessao: Session) -> Iterator[Usuario]:
    linha = _criar_usuario(sessao, ativo=False)
    yield linha
    _limpar_usuario(sessao, linha)


def _esqueci(api: TestClient, email: str) -> Response:
    return api.post("/api/auth/senha/esqueci", json={"email": email})


def _redefinir(api: TestClient, token: str, nova_senha: str) -> Response:
    return api.post("/api/auth/senha/redefinir", json={"token": token, "nova_senha": nova_senha})


def _login(api: TestClient, email: str, senha: str) -> Response:
    return api.post("/api/auth/login", json={"email": email, "senha": senha})


def _token_do_email(corpo: str) -> str:
    """Extrai o token do link — é assim que a pessoa o obtém, e é o que se testa."""
    achado = re.search(r"/redefinir-senha\?token=([A-Za-z0-9_-]+)", corpo)
    assert achado is not None, f"link de redefinição ausente no corpo do e-mail: {corpo!r}"
    return achado.group(1)


def _tokens_do_usuario(session: Session, usuario: Usuario) -> int:
    session.expire_all()
    total = session.scalar(
        select(func.count())
        .select_from(TokenRecuperacao)
        .where(TokenRecuperacao.usuario_id == usuario.id)
    )
    return int(total or 0)


# --- 1. e-mail cadastrado recebe o link ---------------------------------------


def test_esqueci_com_email_cadastrado_envia_um_email_com_o_link(
    api: TestClient, provider: ProviderFalso, usuario: Usuario
) -> None:
    resposta = _esqueci(api, usuario.email)

    assert resposta.status_code == 204
    (destinatario, assunto, corpo) = provider.enviados[0]
    assert len(provider.enviados) == 1
    assert destinatario == usuario.email
    assert assunto == ASSUNTO_RECUPERACAO
    assert "http://localhost:3000/redefinir-senha?token=" in corpo
    assert _token_do_email(corpo)


# --- 2. e-mail inexistente responde idêntico ----------------------------------


def test_esqueci_com_email_inexistente_responde_igual_e_nao_envia(
    api: TestClient, provider: ProviderFalso, usuario: Usuario
) -> None:
    """Critério de aceite que fecha a enumeração: os dois corpos são comparados
    **entre si**, não com um literal — o que precisa valer é a
    indistinguibilidade, não o formato de hoje."""
    cadastrado = _esqueci(api, usuario.email)
    inexistente = _esqueci(api, f"ninguem-{uuid.uuid4()}@teste.local")

    assert cadastrado.status_code == inexistente.status_code == 204
    assert cadastrado.content == inexistente.content
    # Um e-mail só: o do cadastrado.
    assert len(provider.enviados) == 1


# --- 3. usuário inativo responde idêntico -------------------------------------


def test_esqueci_com_usuario_inativo_responde_igual_e_nao_envia(
    api: TestClient, provider: ProviderFalso, usuario: Usuario, usuario_inativo: Usuario
) -> None:
    """Quem saiu da operação não volta por este caminho — e nem é anunciado."""
    cadastrado = _esqueci(api, usuario.email)
    inativo = _esqueci(api, usuario_inativo.email)

    assert cadastrado.status_code == inativo.status_code == 204
    assert cadastrado.content == inativo.content
    assert [destinatario for destinatario, _, _ in provider.enviados] == [usuario.email]


# --- 4. teto de envios por hora ------------------------------------------------


def test_teto_por_hora_para_de_enviar_e_continua_respondendo_204(
    settings: Settings, provider: ProviderFalso, usuario: Usuario
) -> None:
    with _cliente(settings, provider, senha_reset_max_por_hora=1) as api:
        primeira = _esqueci(api, usuario.email)
        segunda = _esqueci(api, usuario.email)

    assert primeira.status_code == segunda.status_code == 204
    assert primeira.content == segunda.content
    assert len(provider.enviados) == 1


# --- 5. provider não configurado ------------------------------------------------


def test_sem_smtp_configurado_responde_204_sem_enviar_e_sem_estourar(
    settings: Settings, sessao: Session, usuario: Usuario
) -> None:
    """Recuperação desligada é modo de operação legítimo, não erro. Nem token é
    emitido: ninguém receberia o link, e o pedido queimaria uma vaga do teto."""
    with _cliente(settings, None) as api:
        resposta = _esqueci(api, usuario.email)

    assert resposta.status_code == 204
    assert _tokens_do_usuario(sessao, usuario) == 0


# --- 6. falha de envio não vira 500 ---------------------------------------------


def test_falha_de_envio_responde_204_e_mantem_o_token_emitido(
    settings: Settings, sessao: Session, usuario: Usuario
) -> None:
    """500 aqui só aconteceria para e-mail que existe — e o status voltaria a
    dizer quem está cadastrado, reabrindo a enumeração que a #30 fechou."""
    provider = ProviderFalso(erro=EnvioEmailError("servidor fora do ar"))

    with _cliente(settings, provider) as api:
        resposta = _esqueci(api, usuario.email)

    assert resposta.status_code == 204
    assert _tokens_do_usuario(sessao, usuario) == 1


# --- 7. redefinir troca a senha --------------------------------------------------


def test_redefinir_com_o_token_do_email_troca_a_senha(
    api: TestClient, provider: ProviderFalso, usuario: Usuario
) -> None:
    assert _esqueci(api, usuario.email).status_code == 204
    token = _token_do_email(provider.enviados[0][2])

    assert _redefinir(api, token, SENHA_NOVA).status_code == 204

    assert _login(api, usuario.email, SENHA_DE_TESTE).status_code == 401
    assert _login(api, usuario.email, SENHA_NOVA).status_code == 200


# --- 8. redefinir revoga as sessões abertas ---------------------------------------


def test_redefinir_revoga_as_sessoes_abertas_do_usuario(
    api: TestClient, provider: ProviderFalso, usuario: Usuario
) -> None:
    """É o ponto da recuperação: trocar a senha sem derrubar as sessões abertas
    deixaria o invasor dentro, com o cookie que ele já tem."""
    assert _login(api, usuario.email, SENHA_DE_TESTE).status_code == 200
    assert api.get("/api/auth/eu").status_code == 200

    assert _esqueci(api, usuario.email).status_code == 204
    token = _token_do_email(provider.enviados[0][2])
    assert _redefinir(api, token, SENHA_NOVA).status_code == 204

    # Mesmo cookie, guardado pelo `TestClient` desde o login acima.
    assert api.get("/api/auth/eu").status_code == 401


# --- 9. uso único ------------------------------------------------------------------


def test_token_usado_duas_vezes_falha_na_segunda(
    api: TestClient, provider: ProviderFalso, usuario: Usuario
) -> None:
    assert _esqueci(api, usuario.email).status_code == 204
    token = _token_do_email(provider.enviados[0][2])
    assert _redefinir(api, token, SENHA_NOVA).status_code == 204

    reuso = _redefinir(api, token, "outra-senha-bem-longa")

    assert reuso.status_code == 422
    assert reuso.json()["error"]["mensagem"] == MENSAGEM_TOKEN_INVALIDO


# --- 10 e 11. token expirado e token inexistente respondem idêntico ----------------


def test_token_expirado_e_token_inexistente_respondem_o_mesmo_422(
    api: TestClient, sessao: Session, settings: Settings, usuario: Usuario
) -> None:
    """Os dois corpos são comparados entre si: distinguir "expirado" de "nunca
    existiu" diria a quem tem um link velho por que ele não vale."""
    agora = datetime.now(UTC)
    token = recuperacao.emitir_token(sessao, usuario, settings=settings, agora=agora)
    assert token is not None
    pedido = sessao.scalars(
        select(TokenRecuperacao).where(
            TokenRecuperacao.token_hash == recuperacao.hash_do_token(token)
        )
    ).one()
    pedido.expires_at = agora - timedelta(minutes=1)
    sessao.commit()

    expirado = _redefinir(api, token, SENHA_NOVA)
    inexistente = _redefinir(api, "token-que-nunca-existiu", SENHA_NOVA)

    assert expirado.status_code == inexistente.status_code == 422
    assert expirado.json() == inexistente.json()
    assert expirado.json()["error"]["mensagem"] == MENSAGEM_TOKEN_INVALIDO


# --- 12. senha fraca não consome o token -------------------------------------------


def test_senha_curta_demais_e_recusada_sem_consumir_o_token(
    api: TestClient, provider: ProviderFalso, usuario: Usuario, settings: Settings
) -> None:
    """Consumir o token numa validação que falhou obrigaria a pessoa a pedir
    outro e-mail por ter digitado uma senha curta."""
    assert _esqueci(api, usuario.email).status_code == 204
    token = _token_do_email(provider.enviados[0][2])

    recusada = _redefinir(api, token, SENHA_CURTA)

    assert recusada.status_code == 422
    mensagem = recusada.json()["error"]["mensagem"]
    assert str(settings.senha_minima_caracteres) in mensagem
    assert "caracteres" in mensagem

    # O token continua valendo: é o que separa "senha recusada" de "recomece do
    # e-mail".
    assert _redefinir(api, token, SENHA_NOVA).status_code == 204
    assert _login(api, usuario.email, SENHA_NOVA).status_code == 200
