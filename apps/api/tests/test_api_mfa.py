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

**Todo teste deste módulo roda com a cifra do segredo em repouso LIGADA** (ADR
0008), pela fixture autouse `cifra_ligada`. A chave vem de uma variável de
ambiente de verdade, e não de um override de dependency: `db/cifra.py` a resolve
por `get_settings()` — dentro do SQLAlchemy não há request nem
`dependency_overrides` por perto —, então um teste que mexesse só no override
estaria medindo uma configuração que a coluna nunca leria.

Nenhum teste daqui imprime senha, segredo, chave ou código de recuperação.
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
from tests.conftest import (
    AUTH_HEADERS,
    TEST_API_KEY,
    TEST_API_KEY_PAPEIS,
    TEST_MFA_SECRET_KEY,
    TEST_MFA_SECRET_KEY_ANTIGA,
    configurar_chaves_mfa,
)

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
            "api_key_papeis": TEST_API_KEY_PAPEIS,
            "environment": "local",
            "login_atraso_base_segundos": 0.0,
            "login_atraso_maximo_segundos": 0.0,
        }
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def cifra_ligada(chave_mfa: str) -> str:
    """A cifra do segredo em repouso ligada em todo teste deste módulo.

    É `autouse` porque a coluna `usuarios.mfa_secret` recusa gravar sem chave
    (ADR 0008): sem esta fixture, cada fixture de usuário com MFA quebraria por
    configuração, e não pelo que o teste quer provar. Quem precisa de outra
    configuração — nenhuma chave, ou duas — a troca com `configurar_chaves_mfa`
    dentro do próprio teste.
    """
    return chave_mfa


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
def usuario(sessao: Session, cifra_ligada: str) -> Iterator[Usuario]:
    """Usuário **sem** MFA — o caminho de quem nunca ativou nada."""
    linha = _criar_usuario(sessao)
    yield linha
    _limpar_usuario(sessao, linha)


@pytest.fixture
def usuario_com_mfa(sessao: Session, cifra_ligada: str) -> Iterator[tuple[Usuario, str]]:
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
            "api_key_papeis": TEST_API_KEY_PAPEIS,
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


# --- 13-20. reemissão dos códigos de recuperação (issue #39) ------------------

REEMITIR = "/api/auth/mfa/reemitir-codigos"


def _reemitir(api: TestClient, *, senha: str, codigo: str) -> Response:
    return api.post(REEMITIR, json={"senha": senha, "codigo": codigo})


def _hashes_de(sessao: Session, usuario: Usuario) -> set[str]:
    return set(
        sessao.scalars(
            select(CodigoRecuperacaoMfa.codigo_hash).where(
                CodigoRecuperacaoMfa.usuario_id == usuario.id
            )
        ).all()
    )


def test_reemitir_devolve_lista_nova_sem_tocar_no_segredo(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    """O caminho feliz inteiro: lista nova, segredo intacto, só hash no banco.

    O segredo intacto é o ponto da feature — reemitir código de recuperação
    **não** é rotacionar o segundo fator, e obrigar a reconfigurar o aplicativo
    devolveria o custo que esta rota existe para eliminar.
    """
    segredo, antigos = _ativar_mfa(api, usuario)

    resposta = _reemitir(api, senha=SENHA_DE_TESTE, codigo=_codigo(segredo, delta=1))

    assert resposta.status_code == 200
    novos = resposta.json()["codigos"]
    assert len(novos) == CODIGOS_ESPERADOS
    assert len(set(novos)) == CODIGOS_ESPERADOS
    assert set(novos).isdisjoint(antigos)

    sessao.refresh(usuario)
    # O segredo é o mesmo: o aplicativo autenticador da pessoa continua valendo.
    assert usuario.mfa_secret == segredo
    assert usuario.mfa_ativado is True

    # E o banco continua guardando só hash — nem os novos nem os antigos em claro.
    hashes = _hashes_de(sessao, usuario)
    assert len(hashes) == CODIGOS_ESPERADOS
    for codigo in [*novos, *antigos]:
        assert codigo not in hashes


def test_reemitir_mata_os_codigos_antigos_inclusive_os_nunca_usados(
    api: TestClient, usuario: Usuario
) -> None:
    """Critério central: um código antigo que sobrevivesse faria a reemissão
    **aumentar** a superfície de ataque em vez de reduzi-la — quem troca a lista
    porque ela pode ter vazado ficaria com a lista vazada valendo do mesmo jeito.
    """
    segredo, antigos = _ativar_mfa(api, usuario)
    assert _reemitir(api, senha=SENHA_DE_TESTE, codigo=_codigo(segredo, delta=1)).status_code == (
        200
    )

    api.cookies.clear()
    assert _login(api, usuario).status_code == 200
    # `antigos[0]` nunca foi usado: morreu pela reemissão, não pelo uso único.
    recusado = api.post("/api/auth/mfa/verificar", json={"codigo": antigos[0]})

    assert recusado.status_code == 401
    assert api.get("/api/operadoras").status_code == 401
    # E não é só o primeiro da lista: a lista inteira morreu.
    assert api.post("/api/auth/mfa/verificar", json={"codigo": antigos[-1]}).status_code == 401


def test_codigo_reemitido_funciona_no_login_e_continua_de_uso_unico(
    api: TestClient, usuario: Usuario
) -> None:
    """A lista nova precisa ser tão boa quanto a da ativação — inclusive no uso
    único, que é o que impede um código anotado de virar credencial permanente."""
    segredo, _ = _ativar_mfa(api, usuario)
    novos = _reemitir(api, senha=SENHA_DE_TESTE, codigo=_codigo(segredo, delta=1)).json()["codigos"]

    api.cookies.clear()
    assert _login(api, usuario).status_code == 200
    primeira = api.post("/api/auth/mfa/verificar", json={"codigo": novos[0]})

    assert primeira.status_code == 200
    assert api.get("/api/operadoras").status_code == 200

    assert _login(api, usuario).status_code == 200
    assert api.post("/api/auth/mfa/verificar", json={"codigo": novos[0]}).status_code == 401
    # O resto da lista continua valendo: o uso único é do código, não da lista.
    assert api.post("/api/auth/mfa/verificar", json={"codigo": novos[1]}).status_code == 200


def test_reemitir_recusa_senha_errada_e_codigo_errado_sem_dizer_qual(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    """Duas mensagens diriam a quem está com a sessão de outra pessoa qual
    metade da credencial ele já tem — e esta rota emite bypass do segundo fator."""
    segredo, _ = _ativar_mfa(api, usuario)
    codigo_valido = _codigo(segredo, delta=1)
    antes = _hashes_de(sessao, usuario)

    senha_errada = _reemitir(api, senha="nao-e-a-senha", codigo=codigo_valido)
    codigo_errado = _reemitir(api, senha=SENHA_DE_TESTE, codigo=_codigo_errado(segredo))

    assert senha_errada.status_code == 422
    assert codigo_errado.status_code == 422
    assert senha_errada.json() == codigo_errado.json()
    # Nada foi trocado: a recusa não pode custar a lista de quem digitou errado.
    assert _hashes_de(sessao, usuario) == antes


def test_reemitir_nao_aceita_o_mesmo_codigo_totp_duas_vezes(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    """Anti-replay: sem gravar o passo aceito, o código visto por cima do ombro
    valeria durante toda a janela de tolerância — aqui, para emitir oito
    credenciais de bypass."""
    segredo, _ = _ativar_mfa(api, usuario)
    codigo = _codigo(segredo, delta=1)

    primeira = _reemitir(api, senha=SENHA_DE_TESTE, codigo=codigo)
    depois_da_primeira = _hashes_de(sessao, usuario)
    segunda = _reemitir(api, senha=SENHA_DE_TESTE, codigo=codigo)

    assert primeira.status_code == 200
    assert segunda.status_code == 422
    sessao.refresh(usuario)
    assert usuario.mfa_ultimo_passo is not None
    # A lista emitida na primeira chamada sobrevive à recusa da segunda.
    assert _hashes_de(sessao, usuario) == depois_da_primeira


def test_reemitir_sem_mfa_ativo_responde_409(api: TestClient, usuario: Usuario) -> None:
    """Sem segundo fator não há o que reemitir — mesmo espírito do 409 de
    `/mfa/iniciar` com MFA já ativado."""
    assert _login(api, usuario).status_code == 200

    resposta = _reemitir(api, senha=SENHA_DE_TESTE, codigo="000000")

    assert resposta.status_code == 409


def test_reemitir_com_chave_de_maquina_responde_403(api: TestClient) -> None:
    """Chave de máquina não tem segundo fator para reemitir: 403, e não 401 — a
    credencial é válida, a operação é que não se aplica a ela."""
    api.cookies.clear()

    resposta = api.post(REEMITIR, json={"senha": "x", "codigo": "000000"}, headers=AUTH_HEADERS)

    assert resposta.status_code == 403


def test_reemitir_sem_sessao_responde_401(api: TestClient) -> None:
    api.cookies.clear()

    resposta = _reemitir(api, senha=SENHA_DE_TESTE, codigo="000000")

    assert resposta.status_code == 401


def test_reemitir_e_travada_por_tentativa_e_nao_so_contada(
    settings: Settings, sessao: Session, usuario: Usuario
) -> None:
    """Regressão: a rota que emite bypass do segundo fator não pode ser sondada
    de graça.

    O freio é o mesmo de `/mfa/verificar` — `avaliar_bloqueio` antes de conferir,
    `registrar_tentativa` nos dois desfechos —, e a consequência precisa estar
    dita em algum lugar que reprova se ela mudar: **as falhas daqui contam em
    `tentativas_login` com o e-mail da pessoa e trancam o login dela**.
    """
    limiar = 3
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "api_key_papeis": TEST_API_KEY_PAPEIS,
            "environment": "local",
            "login_atraso_base_segundos": 0.0,
            "login_atraso_maximo_segundos": 0.0,
            "login_falhas_para_travar_conta": limiar,
        }
    )
    try:
        cliente = TestClient(app)
        segredo, _ = _ativar_mfa(cliente, usuario)
        codigo = _codigo(segredo, delta=1)

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
        for _ in range(limiar):
            assert _reemitir(cliente, senha="nao-e-a-senha", codigo=codigo).status_code == 422
        assert _falhas() == antes + limiar

        bloqueada = _reemitir(cliente, senha=SENHA_DE_TESTE, codigo=codigo)

        assert bloqueada.status_code == 429
        assert bloqueada.headers["retry-after"]
    finally:
        app.dependency_overrides.clear()


# --- 21. o freio que faltava em `/mfa/desativar` -------------------------------


def test_desativar_e_travada_por_tentativa_e_nao_so_contada(
    settings: Settings, usuario: Usuario
) -> None:
    """Regressão de segurança: `/mfa/desativar` precisa CONSULTAR o bloqueio.

    A rota exigia senha e código desde sempre, e isso parecia bastar. Não
    bastava: sem freio, quem tivesse a senha sondava os 10⁶ códigos de seis
    dígitos sem 429, sem atraso e sem deixar linha em `tentativas_login` — e o
    prêmio aqui é maior que o de `/mfa/verificar`, que sempre foi protegida.
    Lá o sucesso dá uma sessão; aqui **desliga o segundo fator** da conta.

    Registrar sem consultar não resolveria: as falhas trancariam apenas o
    *login seguinte*, que o atacante não precisa fazer — ele já está com a
    sessão na mão e sonda até ela expirar.
    """
    limiar = 3
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "api_key_papeis": TEST_API_KEY_PAPEIS,
            "environment": "local",
            "login_atraso_base_segundos": 0.0,
            "login_atraso_maximo_segundos": 0.0,
            "login_falhas_para_travar_conta": limiar,
        }
    )
    try:
        cliente = TestClient(app)
        segredo, _ = _ativar_mfa(cliente, usuario)

        for _ in range(limiar):
            recusado = cliente.post(
                "/api/auth/mfa/desativar",
                json={"senha": SENHA_DE_TESTE, "codigo": _codigo_errado(segredo)},
            )
            assert recusado.status_code == 422

        bloqueada = cliente.post(
            "/api/auth/mfa/desativar",
            json={"senha": SENHA_DE_TESTE, "codigo": _codigo_errado(segredo)},
        )

        assert bloqueada.status_code == 429
        assert bloqueada.headers["retry-after"]
        # E o segundo fator continua de pé: a sondagem não conseguiu desligá-lo.
        assert cliente.post("/api/auth/mfa/iniciar").status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_desativar_registra_a_falha_em_tentativas_login(
    api: TestClient, usuario: Usuario, sessao: Session
) -> None:
    """A falha aqui precisa deixar rastro, e não só ser recusada.

    Sem a linha em `tentativas_login`, uma sondagem contra o segundo fator não
    aparece em lugar nenhum: nem trava, nem alimenta a trava do login, nem é
    investigável depois. É a metade da correção que o 429 sozinho não entrega.
    """
    segredo, _ = _ativar_mfa(api, usuario)
    antes = sessao.scalar(
        select(func.count())
        .select_from(TentativaLogin)
        .where(TentativaLogin.email_tentado == usuario.email, ~TentativaLogin.sucesso)
    )

    recusado = api.post(
        "/api/auth/mfa/desativar",
        json={"senha": "senha-errada-de-proposito", "codigo": _codigo(segredo, delta=1)},
    )

    assert recusado.status_code == 422
    depois = sessao.scalar(
        select(func.count())
        .select_from(TentativaLogin)
        .where(TentativaLogin.email_tentado == usuario.email, ~TentativaLogin.sucesso)
    )
    assert depois == (antes or 0) + 1


# --- 21-26. cifra do segredo em repouso (ADR 0008) ----------------------------


def _coluna_crua(sessao: Session, usuario: Usuario) -> str | None:
    """O que está gravado em `usuarios.mfa_secret`, sem passar pelo ORM.

    Ler pelo model devolveria o segredo já decifrado por `SegredoCifrado` — que
    é o ponto do tipo, e por isso mesmo não serve para provar que a coluna está
    cifrada. SQL cru é a única leitura que enxerga o valor de verdade, e é o
    mesmo `select mfa_secret from usuarios` que alguém com um dump faria.
    """
    return sessao.execute(
        text("select mfa_secret from usuarios where id = :id"), {"id": usuario.id}
    ).scalar_one()


def test_a_coluna_guarda_token_fernet_e_nao_o_segredo_em_claro(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    """Critério de aceite 1: o dump não entrega o segundo fator de ninguém.

    Este é o teste que falharia se alguém trocasse o tipo da coluna de volta por
    `String` — o resto da suíte continuaria verde, porque tudo o mais fala com o
    valor já decifrado.
    """
    segredo, _ = _ativar_mfa(api, usuario)

    guardado = _coluna_crua(sessao, usuario)

    assert guardado is not None
    assert guardado.startswith("gAAAAA")
    assert guardado != segredo
    assert segredo not in guardado

    # E o código continua enxergando o segredo em claro: a cifra é em repouso,
    # não uma mudança do que o resto do sistema manipula.
    sessao.expire(usuario)
    assert usuario.mfa_secret == segredo


def test_o_segredo_da_resposta_de_iniciar_continua_em_claro(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    """A cifra é em REPOUSO, não em trânsito — e não podia ser diferente.

    É este `secret` que a pessoa digita no app autenticador quando o QR code não
    lê, e é dele que `otpauth_uri` é derivado. Devolver o token Fernet aqui
    cadastraria no aplicativo um segredo que não gera código nenhum.
    """
    assert _login(api, usuario).status_code == 200

    corpo = api.post("/api/auth/mfa/iniciar").json()

    segredo = corpo["secret"]
    assert not segredo.startswith("gAAAAA")
    assert segredo in corpo["otpauth_uri"]
    # O mesmo segredo, cifrado, é o que foi para o banco.
    guardado = _coluna_crua(sessao, usuario)
    assert guardado is not None and guardado.startswith("gAAAAA")
    # E ele funciona de verdade: é o que o aplicativo autenticador vai usar.
    assert api.post("/api/auth/mfa/confirmar", json={"codigo": _codigo(segredo)}).status_code == 200


def test_sem_chave_iniciar_responde_503_e_nada_e_gravado(
    api: TestClient,
    sessao: Session,
    usuario: Usuario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critério de aceite 3, e o comportamento central do ADR 0008.

    Um sistema que degrada em silêncio para texto claro é pior que um que
    recusa: quem ativou o segundo fator achando que estava protegido não teria
    como descobrir que não estava. 503 e não 500 porque é indisponibilidade de
    configuração do servidor, não erro de quem chama — a mesma requisição
    passará a funcionar assim que a chave existir.
    """
    assert _login(api, usuario).status_code == 200
    configurar_chaves_mfa(monkeypatch, "")

    resposta = api.post("/api/auth/mfa/iniciar")

    assert resposta.status_code == 503
    # A resposta não conta a configuração do servidor a quem chama: o nome da
    # variável vive no log e no warning do boot.
    assert "MFA_SECRET_KEYS" not in resposta.text

    # E NADA foi gravado: nem cifrado, nem em claro.
    sessao.expire_all()
    assert _coluna_crua(sessao, usuario) is None


def test_sem_chave_quem_nao_usa_mfa_continua_logando(
    api: TestClient, usuario: Usuario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A falta da chave para UM recurso opcional, e não a API inteira.

    É a razão de `main._validar_configuracao_de_mfa` avisar em vez de recusar
    subir: derrubar todo mundo por causa de um segundo fator que a maioria nem
    ativou seria trocar uma indisponibilidade parcial por uma total.
    """
    configurar_chaves_mfa(monkeypatch, "")

    resposta = _login(api, usuario)

    assert resposta.status_code == 200
    assert resposta.json()["email"] == usuario.email
    assert api.get("/api/operadoras").status_code == 200


def test_segredo_cifrado_pela_chave_antiga_continua_verificando_apos_a_rotacao(
    api: TestClient, sessao: Session, usuario: Usuario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critério de aceite 4: rotação sem downtime e sem script de emergência.

    Sem `MultiFernet`, trocar a chave exigiria reescrever a coluna inteira antes
    do deploy — e o passo entre uma coisa e outra é justamente onde o login de
    quem tem MFA para de funcionar.
    """
    # A conta ativa o segundo fator com a chave que vai ser aposentada.
    configurar_chaves_mfa(monkeypatch, TEST_MFA_SECRET_KEY_ANTIGA)
    segredo, _ = _ativar_mfa(api, usuario)
    cifrado_com_a_antiga = _coluna_crua(sessao, usuario)
    api.cookies.clear()

    # A rotação: chave nova na frente, antiga ainda na lista.
    configurar_chaves_mfa(monkeypatch, f"{TEST_MFA_SECRET_KEY},{TEST_MFA_SECRET_KEY_ANTIGA}")

    assert _login(api, usuario).status_code == 200
    verificar = api.post("/api/auth/mfa/verificar", json={"codigo": _codigo(segredo, delta=1)})

    assert verificar.status_code == 200
    assert api.get("/api/auth/eu").status_code == 200
    # A linha NÃO é recifrada de carona: rotacionar a chave não reescreve dado
    # que ninguém tocou, e é por isso que a antiga precisa ficar na lista.
    sessao.expire_all()
    assert _coluna_crua(sessao, usuario) == cifrado_com_a_antiga


def test_codigo_de_recuperacao_loga_quem_esta_com_o_segredo_ilegivel(
    api: TestClient, sessao: Session, usuario: Usuario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critério de aceite 5, e a armadilha que a cifra cria.

    Perder `MFA_SECRET_KEYS` torna ilegível o segundo fator de quem o tem ativo.
    A saída é o código de recuperação — hasheado em Argon2id e independente do
    segredo —, e ela só continua existindo porque `SegredoCifrado` degrada a
    leitura para `None` em vez de levantar: com `None`, `/mfa/verificar` pula o
    TOTP e cai no código de recuperação, que é o caminho de volta.

    Levantar na leitura derrubaria com 500 justamente a rota de emergência, e a
    conta ficaria sem saída nenhuma.
    """
    _, codigos = _ativar_mfa(api, usuario)
    api.cookies.clear()

    # A chave se perdeu (ou foi rotacionada errado): o segredo não abre mais.
    configurar_chaves_mfa(monkeypatch, TEST_MFA_SECRET_KEY_ANTIGA)
    sessao.expire_all()
    assert usuario.mfa_secret is None
    assert usuario.mfa_ativado is True

    assert _login(api, usuario).json() == {"mfa_pendente": True}
    resposta = api.post("/api/auth/mfa/verificar", json={"codigo": codigos[0]})

    assert resposta.status_code == 200
    assert resposta.json()["email"] == usuario.email
    assert api.get("/api/auth/eu").status_code == 200


# --- 27-32. desativar com o segredo ilegível (ADR 0008, issue #39) -------------

# O ADR 0008 registrou como trabalho futuro a consequência mais dura da cifra:
# com a chave perdida, `/mfa/desativar` respondia 409 e quem entrava pelo código
# de recuperação ficava com um segundo fator quebrado e sem saída pela API — e
# ainda recebia uma mensagem que mentia para ela ("o segundo fator não está
# ativado nesta conta"), porque `mfa_ativado` continua `True`. Os testes abaixo
# são o fechamento dessa lacuna.


def _perder_a_chave(monkeypatch: pytest.MonkeyPatch, sessao: Session, usuario: Usuario) -> None:
    """Tira de cena a chave que cifrou o segredo — o pior dia do ADR 0008.

    É o que acontece quando `MFA_SECRET_KEYS` se perde ou quando uma rotação
    remove a chave antiga cedo demais: a coluna continua preenchida no banco e a
    leitura degrada para `None` (`db/cifra.SegredoCifrado`), com a flag ainda
    ligada.
    """
    configurar_chaves_mfa(monkeypatch, TEST_MFA_SECRET_KEY_ANTIGA)
    sessao.expire_all()
    assert usuario.mfa_secret is None
    assert usuario.mfa_ativado is True


def _codigo_de_recuperacao_errado(codigos: list[str]) -> str:
    """Um código com a forma certa que **não** está na lista — sem depender de sorte.

    Mesmo cuidado de `_codigo_errado`: sortear um valor e torcer para ele não
    colidir dá um teste que falha na máquina de outra pessoa e não se reproduz.
    """
    for numero in range(len(codigos) + 1):
        candidato = f"{numero:05d}-{numero:05d}"
        if candidato not in codigos:
            return candidato
    raise AssertionError("nenhum código sobrou fora da lista")


def _codigos_usados(sessao: Session, usuario: Usuario) -> int:
    return (
        sessao.scalar(
            select(func.count())
            .select_from(CodigoRecuperacaoMfa)
            .where(
                CodigoRecuperacaoMfa.usuario_id == usuario.id,
                CodigoRecuperacaoMfa.used_at.is_not(None),
            )
        )
        or 0
    )


def test_com_o_segredo_ilegivel_senha_e_codigo_de_recuperacao_desativam(
    api: TestClient, sessao: Session, usuario: Usuario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saída para quem perdeu a chave da cifra, pelo caminho que ela percorre.

    Continuam sendo **dois fatores**, e não há degradação: o código de
    recuperação já é a credencial que pula o segundo fator no login, então quem
    tem senha e código de recuperação já entra na conta. O que muda é que agora
    ele também consegue sair do estado quebrado.
    """
    _, codigos = _ativar_mfa(api, usuario)
    api.cookies.clear()
    _perder_a_chave(monkeypatch, sessao, usuario)

    # A pessoa entra pelo único caminho que sobrou: o código de recuperação.
    assert _login(api, usuario).json() == {"mfa_pendente": True}
    assert api.post("/api/auth/mfa/verificar", json={"codigo": codigos[0]}).status_code == 200

    resposta = api.post(
        "/api/auth/mfa/desativar", json={"senha": SENHA_DE_TESTE, "codigo": codigos[1]}
    )

    assert resposta.status_code == 204
    sessao.expire_all()
    assert usuario.mfa_ativado is False
    assert usuario.mfa_ultimo_passo is None
    # A coluna precisa ficar NULL DE VERDADE, e não só ler `None`: com o segredo
    # ilegível o atributo já vinha `None` do banco, e um `= None` sobre `None`
    # não é mudança nenhuma para o SQLAlchemy — sem `flag_modified`, o token que
    # não abre continuaria gravado depois de o MFA ter sido desligado.
    assert _coluna_crua(sessao, usuario) is None
    restantes = sessao.scalar(
        select(func.count())
        .select_from(CodigoRecuperacaoMfa)
        .where(CodigoRecuperacaoMfa.usuario_id == usuario.id)
    )
    assert restantes == 0

    # E o login volta a ser de uma etapa: a conta saiu do estado quebrado.
    api.cookies.clear()
    volta = _login(api, usuario)
    assert volta.status_code == 200
    assert volta.json()["email"] == usuario.email


def test_senha_errada_nao_queima_o_codigo_de_recuperacao(
    api: TestClient, sessao: Session, usuario: Usuario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A armadilha do caminho novo: `consumir_codigo_recuperacao` CONSOME.

    No caminho do TOTP os dois fatores são conferidos sempre, e é inofensivo —
    verificar TOTP não gasta nada. Aqui não: conferir o código antes da senha
    faria um erro de digitação na senha queimar um código de uma lista finita,
    que é a última reserva de quem já está sem o segundo fator.
    """
    _, codigos = _ativar_mfa(api, usuario)
    _perder_a_chave(monkeypatch, sessao, usuario)

    recusado = api.post(
        "/api/auth/mfa/desativar", json={"senha": "nao-e-a-senha", "codigo": codigos[0]}
    )

    assert recusado.status_code == 422
    sessao.expire_all()
    assert _codigos_usados(sessao, usuario) == 0
    assert usuario.mfa_ativado is True

    # E a prova de que ele não foi gasto: o MESMO código continua desativando.
    aceito = api.post(
        "/api/auth/mfa/desativar", json={"senha": SENHA_DE_TESTE, "codigo": codigos[0]}
    )
    assert aceito.status_code == 204


def test_senha_errada_e_codigo_errado_respondem_o_mesmo_422_com_o_segredo_ilegivel(
    api: TestClient, sessao: Session, usuario: Usuario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duas mensagens diriam a quem está com a sessão de outra pessoa qual
    metade da credencial ele já tem — a mesma razão do caminho do TOTP."""
    _, codigos = _ativar_mfa(api, usuario)
    _perder_a_chave(monkeypatch, sessao, usuario)

    senha_errada = api.post(
        "/api/auth/mfa/desativar", json={"senha": "nao-e-a-senha", "codigo": codigos[0]}
    )
    codigo_errado = api.post(
        "/api/auth/mfa/desativar",
        json={"senha": SENHA_DE_TESTE, "codigo": _codigo_de_recuperacao_errado(codigos)},
    )

    assert senha_errada.status_code == codigo_errado.status_code == 422
    assert senha_errada.json() == codigo_errado.json()
    # E o segundo fator continua ligado nos dois casos.
    sessao.expire_all()
    assert usuario.mfa_ativado is True


def test_desativar_sem_mfa_ativado_continua_respondendo_409(
    api: TestClient, usuario: Usuario
) -> None:
    """O 409 não sumiu: ele só deixou de valer para segredo ilegível.

    `mfa_ativado=False` é "não há segundo fator para desligar", e continua vindo
    antes do freio — não olha credencial nenhuma e não custa Argon2.
    """
    assert _login(api, usuario).status_code == 200

    resposta = api.post(
        "/api/auth/mfa/desativar", json={"senha": SENHA_DE_TESTE, "codigo": "123456"}
    )

    assert resposta.status_code == 409


def test_com_o_segredo_legivel_o_codigo_de_recuperacao_nao_desativa(
    api: TestClient, sessao: Session, usuario: Usuario
) -> None:
    """Regressão: o caminho novo não pode vazar para o fluxo de sempre.

    Com o app autenticador funcionando, aceitar código de recuperação aqui
    deixaria quem tem a senha e um código vazado desligar o segundo fator sem
    tocar no celular de ninguém — e o código de recuperação existe justamente
    para o caso em que o TOTP não está disponível.
    """
    segredo, codigos = _ativar_mfa(api, usuario)

    recusado = api.post(
        "/api/auth/mfa/desativar", json={"senha": SENHA_DE_TESTE, "codigo": codigos[0]}
    )

    assert recusado.status_code == 422
    sessao.expire_all()
    assert usuario.mfa_ativado is True
    # E ele não foi consumido: continua valendo onde é aceito (`/mfa/verificar`).
    assert _codigos_usados(sessao, usuario) == 0

    # O TOTP de sempre continua desligando, com o mesmo 204.
    aceito = api.post(
        "/api/auth/mfa/desativar",
        json={"senha": SENHA_DE_TESTE, "codigo": _codigo(segredo, delta=1)},
    )
    assert aceito.status_code == 204
    sessao.expire_all()
    assert usuario.mfa_ativado is False
    assert _coluna_crua(sessao, usuario) is None


def test_o_freio_vale_no_caminho_do_codigo_de_recuperacao(
    settings: Settings, sessao: Session, usuario: Usuario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O freio da issue #33 não pode ter ficado do lado de fora do caminho novo.

    O prêmio aqui é o mesmo do caminho do TOTP — desligar o segundo fator —, e
    um código de recuperação sondável sem 429 seria trocar dez dígitos por seis
    e chamar de proteção.
    """
    limiar = 3
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "api_key_papeis": TEST_API_KEY_PAPEIS,
            "environment": "local",
            "login_atraso_base_segundos": 0.0,
            "login_atraso_maximo_segundos": 0.0,
            "login_falhas_para_travar_conta": limiar,
            # Lista curta de propósito: cada tentativa recusada varre os códigos
            # não usados com um Argon2 por linha, e oito deles por tentativa
            # fariam este teste dormir sem provar nada a mais.
            "mfa_codigos_recuperacao": 3,
        }
    )
    try:
        cliente = TestClient(app)
        _, codigos = _ativar_mfa(cliente, usuario)
        _perder_a_chave(monkeypatch, sessao, usuario)
        errado = _codigo_de_recuperacao_errado(codigos)

        for _ in range(limiar):
            recusado = cliente.post(
                "/api/auth/mfa/desativar", json={"senha": SENHA_DE_TESTE, "codigo": errado}
            )
            assert recusado.status_code == 422

        bloqueada = cliente.post(
            "/api/auth/mfa/desativar", json={"senha": SENHA_DE_TESTE, "codigo": errado}
        )

        assert bloqueada.status_code == 429
        assert bloqueada.headers["retry-after"]
        # E o segundo fator continua de pé: a sondagem não conseguiu desligá-lo.
        sessao.expire_all()
        assert usuario.mfa_ativado is True
    finally:
        app.dependency_overrides.clear()
