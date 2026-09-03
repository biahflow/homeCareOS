"""Testes de integração do bloqueio por tentativa de login (issue #33) — contra
Postgres real (localhost:5434).

O banco é compartilhado com o desenvolvimento: cada teste usa e-mail e IP
únicos (`contexto`) e o teardown apaga só as linhas de `tentativas_login` que
batem com esse e-mail OU esse IP — nunca `TRUNCATE`/`DELETE` geral.

O atraso progressivo é zerado nos overrides de `Settings`
(`login_atraso_base_segundos=0.0`, `login_atraso_maximo_segundos=0.0`): esta
suíte não pode dormir. O teto do atraso já é provado, sem dormir de verdade,
em `tests/test_auth_protecao.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from homecareos.auth import protecao, senhas
from homecareos.auth.router import MENSAGEM_BLOQUEIO
from homecareos.auth.schema import Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import TentativaLogin, Usuario
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from tests.conftest import TEST_API_KEY

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-bloqueio"
SENHA_ERRADA = "senha-digitada-errada"

# Limiares baixos para os testes exercitarem a trava sem precisar de dezenas
# de tentativas. O desenho real (`config.py`) quer conta MUITO mais alto que
# IP; aqui só precisamos de dois números pequenos e distintos.
LIMIAR_IP = 3
LIMIAR_CONTA = 5

# Usados só pelo teste de trava de conta (nº 4): limiar de conta
# propositalmente ABAIXO do de IP, para isolar o caminho de trava de conta sem
# depender de acumular falhas de IP — é conveniência de teste, o oposto da
# configuração real.
LIMIAR_IP_ALTO = 50
LIMIAR_CONTA_BAIXA = 2


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
    """Cliente com atraso zerado, limiares baixos, e controle total do IP.

    `confiar_em_x_forwarded_for=True` é deliberado: sem isto, toda requisição
    do `TestClient` chegaria com o mesmo `client.host`, e não daria para um
    teste isolar "este IP está travado" de "aquele IP está livre" — ver o
    caso 3.
    """
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "environment": "local",
            "login_atraso_base_segundos": 0.0,
            "login_atraso_maximo_segundos": 0.0,
            "login_falhas_para_travar_ip": LIMIAR_IP,
            "login_falhas_para_travar_conta": LIMIAR_CONTA,
            "confiar_em_x_forwarded_for": True,
        }
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def api_conta_baixa(settings: Settings) -> Iterator[TestClient]:
    """Só para o teste de trava de conta (nº 4) — ver o comentário de
    `LIMIAR_IP_ALTO`/`LIMIAR_CONTA_BAIXA` acima."""
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "environment": "local",
            "login_atraso_base_segundos": 0.0,
            "login_atraso_maximo_segundos": 0.0,
            "login_falhas_para_travar_ip": LIMIAR_IP_ALTO,
            "login_falhas_para_travar_conta": LIMIAR_CONTA_BAIXA,
            "confiar_em_x_forwarded_for": True,
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


@pytest.fixture
def contexto(sessao: Session) -> Iterator[tuple[str, str]]:
    """(email, ip) únicos para o teste. Teardown apaga as `tentativas_login`
    que batem com o e-mail OU o IP — nunca um `DELETE` geral: o banco é
    compartilhado com o desenvolvimento (ver a docstring do módulo).
    """
    email = f"protecao-{uuid.uuid4()}@teste.local"
    ip = f"ip-teste-{uuid.uuid4()}"
    yield email, ip
    sessao.execute(
        text("delete from tentativas_login where email_tentado = :email or ip = :ip"),
        {"email": email, "ip": ip},
    )
    sessao.commit()


def _criar_usuario(session: Session, *, email: str) -> Usuario:
    usuario = Usuario(
        nome="Pessoa de Teste - Bloqueio",
        email=email,
        senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
        papel=Papel.CONFERENTE.value,
        ativo=True,
    )
    session.add(usuario)
    session.commit()
    return usuario


def _limpar_usuario(session: Session, usuario_id: uuid.UUID) -> None:
    session.execute(text("delete from sessoes where usuario_id = :id"), {"id": usuario_id})
    session.execute(text("delete from usuarios where id = :id"), {"id": usuario_id})
    session.commit()


@pytest.fixture
def usuario(sessao: Session, contexto: tuple[str, str]) -> Iterator[Usuario]:
    email, _ = contexto
    linha = _criar_usuario(sessao, email=email)
    yield linha
    _limpar_usuario(sessao, linha.id)


def _login(api: TestClient, *, email: str, senha: str, ip: str):  # type: ignore[no-untyped-def]
    return api.post(
        "/api/auth/login",
        json={"email": email, "senha": senha},
        headers={"X-Forwarded-For": ip},
    )


# --- 1. falhas até o limiar de IP travam a origem -----------------------------


def test_ip_com_login_bem_sucedido_na_janela_nao_e_travado(
    api: TestClient, usuario: Usuario, contexto: tuple[str, str]
) -> None:
    """Regressão: IP compartilhado por gente que trabalha não pode ser trancado.

    Atrás de proxy — e o default é `confiar_em_x_forwarded_for=False` — a
    empresa inteira chega com um IP só. Contando falhas cruas, erros de
    digitação somados de toda a equipe trancariam todo mundo no começo do
    turno, que é justamente quando todos logam juntos.

    O que separa a rede da operação do atacante é haver login que funciona:
    quem sonda senha não tem nenhum. Por isso a trava de IP exige, além do
    limiar de falhas, **zero sucessos** daquele IP na janela.
    """
    email, ip = contexto
    assert _login(api, email=email, senha=SENHA_DE_TESTE, ip=ip).status_code == 200

    for _ in range(LIMIAR_IP):
        assert _login(api, email=email, senha=SENHA_ERRADA, ip=ip).status_code == 401

    # Com o limiar de IP já atingido, a regra antiga responderia 429 aqui.
    # Segue 401 (credencial errada) porque houve sucesso na janela.
    # O número de falhas é mantido abaixo de `LIMIAR_CONTA` de propósito: quem
    # protege esta conta é a trava de conta, e ela continua valendo — não é
    # isso que este teste isola.
    assert _login(api, email=email, senha=SENHA_ERRADA, ip=ip).status_code == 401


def test_falhas_ate_o_limiar_de_ip_travam_a_origem(
    api: TestClient, usuario: Usuario, contexto: tuple[str, str], settings: Settings
) -> None:
    email, ip = contexto

    for _ in range(LIMIAR_IP):
        resposta = _login(api, email=email, senha=SENHA_ERRADA, ip=ip)
        assert resposta.status_code == 401

    bloqueada = _login(api, email=email, senha=SENHA_ERRADA, ip=ip)

    assert bloqueada.status_code == 429
    assert bloqueada.headers["retry-after"] == str(settings.login_trava_minutos * 60)
    corpo = bloqueada.json()
    assert corpo["error"]["tipo"] == "too_many_requests"
    assert corpo["error"]["mensagem"] == MENSAGEM_BLOQUEIO
    # Literal do critério de aceite: a mensagem exata importa, não só a
    # constante que a define.
    assert corpo["error"]["mensagem"] == "muitas tentativas de login; tente novamente mais tarde"


# --- 2. e-mail inexistente conta igual -----------------------------------------


def test_email_inexistente_conta_igual_para_travar_o_ip(
    api: TestClient, contexto: tuple[str, str]
) -> None:
    """Critério de aceite que fecha a enumeração: sondar com e-mail que não
    existe trava o IP do mesmo jeito que sondar contra uma conta real."""
    _, ip = contexto
    email_inexistente = f"ninguem-{uuid.uuid4()}@teste.local"

    for _ in range(LIMIAR_IP):
        resposta = _login(api, email=email_inexistente, senha="qualquer-coisa", ip=ip)
        assert resposta.status_code == 401

    bloqueada = _login(api, email=email_inexistente, senha="qualquer-coisa", ip=ip)

    assert bloqueada.status_code == 429


# --- 3. IP diferente segue funcionando -----------------------------------------


def test_ip_diferente_segue_funcionando_com_outro_ip_travado(
    api: TestClient, usuario: Usuario, contexto: tuple[str, str]
) -> None:
    email, ip_travado = contexto
    outro_ip = f"ip-teste-{uuid.uuid4()}"

    for _ in range(LIMIAR_IP):
        _login(api, email=email, senha=SENHA_ERRADA, ip=ip_travado)
    assert _login(api, email=email, senha=SENHA_ERRADA, ip=ip_travado).status_code == 429

    resposta = _login(api, email=email, senha=SENHA_DE_TESTE, ip=outro_ip)

    assert resposta.status_code == 200


# --- 4. trava de conta ----------------------------------------------------------


def test_falhas_contra_a_mesma_conta_travam_a_conta(
    api_conta_baixa: TestClient, usuario: Usuario, contexto: tuple[str, str]
) -> None:
    email, _ = contexto

    for _ in range(LIMIAR_CONTA_BAIXA):
        # IP novo a cada tentativa: o que trava aqui precisa ser a CONTA, não
        # o IP (que fica bem abaixo do próprio limiar alto do fixture).
        resposta = _login(
            api_conta_baixa, email=email, senha=SENHA_ERRADA, ip=f"ip-teste-{uuid.uuid4()}"
        )
        assert resposta.status_code == 401

    bloqueada = _login(
        api_conta_baixa, email=email, senha=SENHA_ERRADA, ip=f"ip-teste-{uuid.uuid4()}"
    )

    assert bloqueada.status_code == 429


# --- 5. resposta idêntica para conta existente e inexistente ------------------


def test_resposta_de_bloqueio_e_identica_para_conta_existente_e_inexistente(
    api: TestClient, usuario: Usuario, contexto: tuple[str, str]
) -> None:
    """Os dois corpos são comparados **entre si**, não com um literal: o que
    precisa valer é a indistinguibilidade, não o texto de hoje."""
    email_existente, ip = contexto
    email_inexistente = f"ninguem-{uuid.uuid4()}@teste.local"

    for _ in range(LIMIAR_IP):
        _login(api, email=email_existente, senha=SENHA_ERRADA, ip=ip)

    bloqueio_conta_existente = _login(api, email=email_existente, senha=SENHA_ERRADA, ip=ip)
    bloqueio_conta_inexistente = _login(api, email=email_inexistente, senha="qualquer-coisa", ip=ip)

    assert bloqueio_conta_existente.status_code == bloqueio_conta_inexistente.status_code == 429
    assert bloqueio_conta_existente.json() == bloqueio_conta_inexistente.json()


# --- 6. sucesso zera o estado ---------------------------------------------------


def test_sucesso_zera_o_contador_daquela_conta_mais_ip(
    api: TestClient,
    sessao: Session,
    usuario: Usuario,
    contexto: tuple[str, str],
    settings: Settings,
) -> None:
    """`falhas_recentes` alimenta o atraso progressivo e não é observável via
    HTTP nesta suíte (atraso zerado — ver a docstring do módulo). É checada
    diretamente aqui, contra tentativas gravadas pelo fluxo real de
    `POST /api/auth/login`."""
    email, ip = contexto
    agora = datetime.now(UTC)

    assert _login(api, email=email, senha=SENHA_ERRADA, ip=ip).status_code == 401
    assert _login(api, email=email, senha=SENHA_ERRADA, ip=ip).status_code == 401
    assert protecao.falhas_recentes(sessao, email=email, ip=ip, settings=settings, agora=agora) == 2

    assert _login(api, email=email, senha=SENHA_DE_TESTE, ip=ip).status_code == 200
    assert protecao.falhas_recentes(sessao, email=email, ip=ip, settings=settings, agora=agora) == 0

    assert _login(api, email=email, senha=SENHA_ERRADA, ip=ip).status_code == 401
    assert protecao.falhas_recentes(sessao, email=email, ip=ip, settings=settings, agora=agora) == 1


# --- 7. credencial correta com o IP travado também bloqueia -------------------


def test_credencial_correta_com_o_ip_travado_tambem_responde_429(
    api: TestClient, usuario: Usuario, contexto: tuple[str, str]
) -> None:
    """O bloqueio é avaliado antes da validação de credencial: nem senha
    certa passa com o IP travado."""
    email, ip = contexto

    for _ in range(LIMIAR_IP):
        _login(api, email=email, senha=SENHA_ERRADA, ip=ip)

    resposta = _login(api, email=email, senha=SENHA_DE_TESTE, ip=ip)

    assert resposta.status_code == 429


# --- 8. limpar_tentativas_antigas -----------------------------------------------


def test_limpar_tentativas_antigas_remove_a_antiga_e_preserva_a_recente(
    sessao: Session, contexto: tuple[str, str]
) -> None:
    email, ip = contexto
    agora = datetime.now(UTC)
    antiga = TentativaLogin(
        email_tentado=email, ip=ip, sucesso=False, created_at=agora - timedelta(hours=2)
    )
    recente = TentativaLogin(email_tentado=email, ip=ip, sucesso=False, created_at=agora)
    sessao.add_all([antiga, recente])
    sessao.commit()
    # Ids capturados antes do expurgo: depois dele, `antiga` foi apagada e
    # `expire_on_commit` faria o acesso a `antiga.id` recarregar a linha —
    # que não existe mais — e levantar `ObjectDeletedError`.
    antiga_id, recente_id = antiga.id, recente.id

    removidas = protecao.limpar_tentativas_antigas(sessao, antes_de=agora - timedelta(hours=1))

    assert removidas >= 1
    assert sessao.get(TentativaLogin, antiga_id) is None
    assert sessao.get(TentativaLogin, recente_id) is not None
