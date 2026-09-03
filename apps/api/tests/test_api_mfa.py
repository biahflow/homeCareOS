"""Testes de integração do segundo fator (issue #35) — contra Postgres real
(localhost:5434).

O banco é compartilhado com o desenvolvimento: cada teste cria usuário com
e-mail único e o teardown apaga **só** o que o teste criou — códigos de
recuperação, sessões, tentativas de login e o usuário. Nunca `TRUNCATE`, nunca
`DELETE` geral, e nenhuma asserção conta linhas sem filtrar pelos próprios
registros.

O atraso progressivo do login é zerado nos overrides de `Settings`: vários
testes erram código de propósito (é o que se quer provar) e a suíte não pode
dormir por isso. O atraso já é provado, sem dormir de verdade, em
`tests/test_auth_protecao.py`.

**Os códigos usados nos testes vêm de passos TOTP explícitos**, e não de
`TOTP.now()`. Depois da ativação, o passo confirmado já está gravado em
`usuarios.mfa_ultimo_passo` — reusar o código daquele passo cairia no
anti-replay, que é exatamente o comportamento correto. Pedir o código do passo
seguinte (`_codigo`, `delta=1`) é o que um usuário de verdade faz: espera trinta
segundos.

Nenhum teste daqui imprime senha, segredo ou código de recuperação.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from homecareos.auth import mfa, senhas
from homecareos.auth.schema import Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import CodigoRecuperacaoMfa, TentativaLogin, Usuario
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-mfa-totp"
CODIGOS_ESPERADOS = 8


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
    """Cliente contra a app real, com o atraso do login zerado.

    `environment="local"` é fixado de propósito: fora de `local` o cookie de
    sessão sai com `Secure`, o `TestClient` fala HTTP e o cookie não seria
    guardado — os testes de sessão pendente passariam a medir a flag em vez do
    segundo fator.
    """
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "environment": "local",
            "login_atraso_base_segundos": 0.0,
            "login_atraso_maximo_segundos": 0.0,
        }
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def _criar_usuario(session: Session, *, mfa_secret: str | None = None) -> Usuario:
    usuario = Usuario(
        nome="Pessoa de Teste - MFA",
        email=f"mfa-{uuid.uuid4()}@teste.local",
        senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
        papel=Papel.CONFERENTE.value,
        ativo=True,
        mfa_secret=mfa_secret,
        mfa_ativado=mfa_secret is not None,
    )
    session.add(usuario)
    session.commit()
    return usuario


def _limpar_usuario(session: Session, usuario: Usuario) -> None:
    """Apaga só o rastro deste usuário — o banco é compartilhado."""
    session.execute(
        text("delete from codigos_recuperacao_mfa where usuario_id = :id"), {"id": usuario.id}
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
    """Usuário **sem** MFA — o caminho de quem nunca ativou nada."""
    linha = _criar_usuario(sessao)
    yield linha
    _limpar_usuario(sessao, linha)


@pytest.fixture
def usuario_com_mfa(sessao: Session) -> Iterator[tuple[Usuario, str]]:
    """Usuário com MFA já ativado, e o segredo dele.

    O segredo é plantado direto no banco (em vez de passar por
    `iniciar`/`confirmar`) para os testes que exercitam o **login** não
    dependerem do passo confirmado na ativação: aqui `mfa_ultimo_passo` começa
    `NULL`, como na conta que acabou de ativar e ainda não logou.
    """
    segredo = mfa.gerar_segredo()
    linha = _criar_usuario(sessao, mfa_secret=segredo)
    yield linha, segredo
    _limpar_usuario(sessao, linha)


def _login(api: TestClient, usuario: Usuario, senha: str = SENHA_DE_TESTE) -> Response:
    return api.post("/api/auth/login", json={"email": usuario.email, "senha": senha})


def _codigo(segredo: str, *, delta: int = 0) -> str:
    """Código TOTP do passo atual deslocado de `delta` passos.

    `delta=1` é o que um usuário de verdade digita depois de esperar trinta
    segundos — e é o que escapa do anti-replay quando o passo atual já foi
    consumido pela ativação. Com a janela padrão (±1 passo) ele continua sendo
    aceito mesmo se o relógio virar o passo entre a geração e a verificação, o
    que torna o teste imune à borda.
    """
    return pyotp.TOTP(segredo).at(datetime.now(UTC) + timedelta(seconds=mfa.PASSO_SEGUNDOS * delta))


def _codigo_errado(segredo: str) -> str:
    """Seis dígitos que não valem em passo nenhum da janela — sem depender de sorte.

    Sortear "000000" daria um teste que falha uma vez em um milhão, na máquina
    de outra pessoa, sem se reproduzir.
    """
    validos = {_codigo(segredo, delta=d) for d in (-2, -1, 0, 1, 2)}
    for numero in range(1_000_000):
        candidato = f"{numero:06d}"
        if candidato not in validos:
            return candidato
    raise AssertionError("nenhum código de seis dígitos sobrou fora da janela")


def _ativar_mfa(api: TestClient, usuario: Usuario) -> tuple[str, list[str]]:
    """Faz o fluxo completo de ativação e devolve `(segredo, códigos)`.

    Ao fim, a sessão do cliente é a sessão **completa** de quem acabou de
    ativar — como acontece de verdade: ninguém é deslogado por ligar o segundo
    fator.
    """
    assert _login(api, usuario).status_code == 200
    iniciar = api.post("/api/auth/mfa/iniciar")
    assert iniciar.status_code == 200
    segredo = iniciar.json()["secret"]

    confirmar = api.post("/api/auth/mfa/confirmar", json={"codigo": _codigo(segredo)})
    assert confirmar.status_code == 200
    return segredo, confirmar.json()["codigos"]


# --- 1. o fluxo antigo continua intacto ---------------------------------------


def test_usuario_sem_mfa_loga_em_uma_etapa(api: TestClient, usuario: Usuario) -> None:
    """Regressão: ligar o segundo fator no sistema não pode mudar o login de
    quem não o ativou."""
    resposta = _login(api, usuario)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["email"] == usuario.email
    assert "mfa_pendente" not in corpo
    assert api.get("/api/auth/eu").status_code == 200


# --- 2-3. ativação ------------------------------------------------------------


def test_ativacao_devolve_os_codigos_de_recuperacao_uma_unica_vez(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    _, codigos = _ativar_mfa(api, usuario)

    assert len(codigos) == CODIGOS_ESPERADOS
    assert len(set(codigos)) == CODIGOS_ESPERADOS

    sessao.refresh(usuario)
    assert usuario.mfa_ativado is True
    assert usuario.mfa_secret is not None

    # O banco guarda só o hash: nenhum código em claro sobrou numa coluna.
    hashes = sessao.scalars(
        select(CodigoRecuperacaoMfa.codigo_hash).where(
            CodigoRecuperacaoMfa.usuario_id == usuario.id
        )
    ).all()
    assert len(hashes) == CODIGOS_ESPERADOS
    for codigo in codigos:
        assert codigo not in hashes


def test_confirmar_com_codigo_errado_responde_422_e_nao_ativa(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    assert _login(api, usuario).status_code == 200
    segredo = api.post("/api/auth/mfa/iniciar").json()["secret"]

    resposta = api.post("/api/auth/mfa/confirmar", json={"codigo": _codigo_errado(segredo)})

    assert resposta.status_code == 422
    sessao.refresh(usuario)
    assert usuario.mfa_ativado is False
    # E nenhum código de recuperação foi gravado por uma ativação que não houve.
    total = sessao.scalar(
        select(func.count())
        .select_from(CodigoRecuperacaoMfa)
        .where(CodigoRecuperacaoMfa.usuario_id == usuario.id)
    )
    assert total == 0


# --- 4-6. login em duas etapas ------------------------------------------------


def test_login_com_mfa_ativo_devolve_pendente_e_nao_os_dados_do_usuario(
    api: TestClient, usuario_com_mfa: tuple[Usuario, str]
) -> None:
    usuario, _ = usuario_com_mfa

    resposta = _login(api, usuario)

    assert resposta.status_code == 200
    assert resposta.json() == {"mfa_pendente": True}
    # Quem parou no primeiro fator ainda não provou quem é: nem o e-mail sai.
    assert usuario.email not in resposta.text
    assert api.cookies.get("homecareos_sessao") is not None


def test_sessao_pendente_nao_acessa_rota_nenhuma(
    api: TestClient, usuario_com_mfa: tuple[Usuario, str]
) -> None:
    """Critério de aceite central: sem esta recusa, o segundo fator seria uma
    tela que dá para pular — a sessão do primeiro passo já abriria a API."""
    usuario, _ = usuario_com_mfa
    assert _login(api, usuario).status_code == 200

    assert api.get("/api/auth/eu").status_code == 401
    assert api.get("/api/operadoras").status_code == 401


def test_verificar_com_codigo_valido_completa_a_sessao(
    api: TestClient, usuario_com_mfa: tuple[Usuario, str]
) -> None:
    usuario, segredo = usuario_com_mfa
    assert _login(api, usuario).status_code == 200
    assert api.get("/api/operadoras").status_code == 401

    resposta = api.post("/api/auth/mfa/verificar", json={"codigo": _codigo(segredo)})

    assert resposta.status_code == 200
    assert resposta.json()["email"] == usuario.email
    # A mesma requisição que dava 401 com o cookie pendente passa a 200 — e é o
    # mesmo cookie: completar a sessão não troca a credencial do navegador.
    assert api.get("/api/operadoras").status_code == 200
    assert api.get("/api/auth/eu").status_code == 200


# --- 7-9. anti-replay, código de recuperação e contagem de falha --------------


def test_o_mesmo_codigo_nao_funciona_duas_vezes(
    api: TestClient, sessao: Session, usuario_com_mfa: tuple[Usuario, str]
) -> None:
    """Sem o anti-replay, quem interceptar o código tem ~90 segundos para reusá-lo."""
    usuario, segredo = usuario_com_mfa
    codigo = _codigo(segredo)

    assert _login(api, usuario).status_code == 200
    assert api.post("/api/auth/mfa/verificar", json={"codigo": codigo}).status_code == 200
    sessao.refresh(usuario)
    assert usuario.mfa_ultimo_passo is not None

    # Novo login, mesmo código: o passo já foi consumido.
    assert _login(api, usuario).status_code == 200
    replay = api.post("/api/auth/mfa/verificar", json={"codigo": codigo})

    assert replay.status_code == 401
    assert api.get("/api/operadoras").status_code == 401


def test_codigo_de_recuperacao_funciona_e_e_de_uso_unico(api: TestClient, usuario: Usuario) -> None:
    _, codigos = _ativar_mfa(api, usuario)
    codigo = codigos[0]

    assert _login(api, usuario).status_code == 200
    primeira = api.post("/api/auth/mfa/verificar", json={"codigo": codigo})

    assert primeira.status_code == 200
    assert api.get("/api/operadoras").status_code == 200

    assert _login(api, usuario).status_code == 200
    segunda = api.post("/api/auth/mfa/verificar", json={"codigo": codigo})

    assert segunda.status_code == 401
    assert api.get("/api/operadoras").status_code == 401

    # E o resto da lista continua valendo: o uso único é do código, não da lista.
    assert api.post("/api/auth/mfa/verificar", json={"codigo": codigos[1]}).status_code == 200


def test_sondagem_do_segundo_fator_e_travada_e_nao_so_contada(
    settings: Settings, usuario_com_mfa: tuple[Usuario, str]
) -> None:
    """Regressão: `/mfa/verificar` precisa CONSULTAR o bloqueio, não só registrar.

    Quem chega nesta rota já tem a senha — o cookie pendente só existe porque o
    primeiro passo passou. O MFA é a última linha, e são seis dígitos.

    Registrando sem consultar, as falhas trancariam apenas o *login seguinte*,
    que o atacante não precisa fazer: ele já está com a sessão pendente na mão e
    sonda até ela expirar (12h por padrão). Com a consulta, a trava de conta
    corta a sondagem no limiar configurado.
    """
    usuario, segredo = usuario_com_mfa
    limiar = 3
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "environment": "local",
            "login_atraso_base_segundos": 0.0,
            "login_atraso_maximo_segundos": 0.0,
            "login_falhas_para_travar_conta": limiar,
        }
    )
    try:
        cliente = TestClient(app)
        assert _login(cliente, usuario).status_code == 200

        for _ in range(limiar):
            assert (
                cliente.post(
                    "/api/auth/mfa/verificar", json={"codigo": _codigo_errado(segredo)}
                ).status_code
                == 401
            )

        bloqueada = cliente.post(
            "/api/auth/mfa/verificar", json={"codigo": _codigo_errado(segredo)}
        )

        assert bloqueada.status_code == 429
        assert bloqueada.headers["retry-after"]
    finally:
        app.dependency_overrides.clear()


def test_codigo_errado_na_verificacao_conta_em_tentativas_login(
    api: TestClient, sessao: Session, usuario_com_mfa: tuple[Usuario, str]
) -> None:
    """São seis dígitos: o freio da issue #33 é o que os protege. Sem esta
    contagem, o segundo fator vira o alvo — e o alvo mais barato do sistema."""
    usuario, segredo = usuario_com_mfa
    assert _login(api, usuario).status_code == 200

    def _falhas() -> int:
        return (
            sessao.scalar(
                select(func.count())
                .select_from(TentativaLogin)
                .where(
                    TentativaLogin.email_tentado == usuario.email,
                    TentativaLogin.sucesso.is_(False),
                )
            )
            or 0
        )

    antes = _falhas()
    resposta = api.post("/api/auth/mfa/verificar", json={"codigo": _codigo_errado(segredo)})

    assert resposta.status_code == 401
    assert _falhas() == antes + 1


def test_verificar_sem_cookie_pendente_responde_401(
    api: TestClient, usuario_com_mfa: tuple[Usuario, str]
) -> None:
    """Sem cookie e com cookie que não é de sessão pendente: o mesmo 401."""
    usuario, segredo = usuario_com_mfa

    sem_cookie = api.post("/api/auth/mfa/verificar", json={"codigo": _codigo(segredo)})
    assert sem_cookie.status_code == 401

    assert _login(api, usuario).status_code == 200
    assert api.post("/api/auth/mfa/verificar", json={"codigo": _codigo(segredo)}).status_code == (
        200
    )

    # Agora a sessão já não é pendente: o mesmo endpoint deixa de enxergá-la.
    repetida = api.post("/api/auth/mfa/verificar", json={"codigo": _codigo(segredo, delta=1)})
    assert repetida.status_code == 401
    assert sem_cookie.json() == repetida.json()


# --- 10-12. gestão ------------------------------------------------------------


def test_desativar_exige_senha_e_codigo_e_devolve_o_login_a_uma_etapa(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    """Com só um dos dois fatores, uma sessão sequestrada desligaria o segundo
    fator sozinha — exatamente o que ele existe para impedir."""
    segredo, _ = _ativar_mfa(api, usuario)

    # O passo da confirmação já foi consumido: o código de agora é o do passo
    # seguinte, como para quem esperou trinta segundos.
    codigo = _codigo(segredo, delta=1)

    so_codigo = api.post("/api/auth/mfa/desativar", json={"codigo": codigo})
    so_senha = api.post("/api/auth/mfa/desativar", json={"senha": SENHA_DE_TESTE})
    assert so_codigo.status_code == 422
    assert so_senha.status_code == 422

    senha_errada = api.post(
        "/api/auth/mfa/desativar", json={"senha": "nao-e-a-senha", "codigo": codigo}
    )
    assert senha_errada.status_code == 422

    sessao.refresh(usuario)
    assert usuario.mfa_ativado is True

    desativar = api.post(
        "/api/auth/mfa/desativar", json={"senha": SENHA_DE_TESTE, "codigo": codigo}
    )
    assert desativar.status_code == 204

    sessao.refresh(usuario)
    assert usuario.mfa_ativado is False
    assert usuario.mfa_secret is None
    assert usuario.mfa_ultimo_passo is None
    codigos_restantes = sessao.scalar(
        select(func.count())
        .select_from(CodigoRecuperacaoMfa)
        .where(CodigoRecuperacaoMfa.usuario_id == usuario.id)
    )
    assert codigos_restantes == 0

    # E o login volta a ser de uma etapa.
    api.cookies.clear()
    volta = _login(api, usuario)
    assert volta.status_code == 200
    assert volta.json()["email"] == usuario.email


def test_iniciar_com_mfa_ja_ativo_responde_409(
    api: TestClient, usuario_com_mfa: tuple[Usuario, str]
) -> None:
    """Substituir o segredo de quem já usa o segundo fator, com uma sessão que
    pode ser sequestrada, seria trocar a credencial por outra sem provar nada."""
    usuario, segredo = usuario_com_mfa
    assert _login(api, usuario).status_code == 200
    assert api.post("/api/auth/mfa/verificar", json={"codigo": _codigo(segredo)}).status_code == (
        200
    )

    assert api.post("/api/auth/mfa/iniciar").status_code == 409


def test_iniciar_de_novo_antes_de_confirmar_substitui_o_segredo(
    api: TestClient, usuario: Usuario
) -> None:
    """Quem perdeu o QR code no meio do cadastro precisa recomeçar — e um
    segredo não confirmado não protege nada."""
    assert _login(api, usuario).status_code == 200

    primeiro = api.post("/api/auth/mfa/iniciar").json()["secret"]
    segundo = api.post("/api/auth/mfa/iniciar").json()["secret"]
    assert primeiro != segundo

    # O código do segredo abandonado não ativa nada.
    assert api.post("/api/auth/mfa/confirmar", json={"codigo": _codigo(primeiro)}).status_code == (
        422
    )
    assert api.post("/api/auth/mfa/confirmar", json={"codigo": _codigo(segundo)}).status_code == 200


def test_chave_de_maquina_nao_configura_segundo_fator(api: TestClient) -> None:
    """Chave de máquina não tem celular nem app autenticador: 403, e não 401 —
    a credencial é válida, a operação é que não se aplica a ela."""
    api.cookies.clear()

    resposta = api.post("/api/auth/mfa/iniciar", headers=AUTH_HEADERS)

    assert resposta.status_code == 403
