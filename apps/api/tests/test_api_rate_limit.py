"""Testes de integração do rate limit das rotas caras (ADR 0005) — Postgres real.

Três cuidados moram neste módulo, e cada um responde a um risco concreto:

**Nenhum teste depende de volume real.** Bater 120 vezes numa rota para provar
um limite de 120 seria uma suíte lenta que ainda por cima mede a máquina, não o
código. Todos os limites são baixados por `app.dependency_overrides[get_settings]`
— o mesmo recurso que `tests/test_api_mfa.py` usa para baixar o limiar de
bloqueio do login.

**Nenhum teste faz chamada paga ao provider de IA.** `POST /api/documentos`
dispara extração síncrona; aqui ele é exercitado de dois jeitos que nunca
chegam ao provider: com os dublês de `tests/fakes.py` no lugar de repositório,
storage e dispatcher (e a asserção de que o dispatcher **não** foi chamado na
requisição bloqueada), e com o limite em zero, caso em que o 429 sai da
dependency e o handler não roda.

**O banco é compartilhado.** Cada teste cria usuário com e-mail único e o
teardown apaga só o que ele criou, filtrando pela própria chave do contador.
Nunca `TRUNCATE`, nunca `DELETE` geral.

O tempo entra por dado, não por relógio: para envelhecer um consumo, o teste
grava `created_at` no passado (é uma coluna comum, o `server_default` só vale
quando ninguém informa valor). Não há freezegun no projeto.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from homecareos.auth import senhas
from homecareos.auth.schema import Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import ConsumoRateLimit, Usuario
from homecareos.db.session import get_sessionmaker
from homecareos.intake.router import (
    get_document_storage,
    get_documento_repository,
    get_extraction_dispatcher,
)
from homecareos.limites.protecao import CHAVE_MAQUINA, JANELA, PREFIXO_USUARIO
from homecareos.limites.schema import Recurso, limites_do_recurso
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY
from tests.fakes import FakeDispatcher, FakeDocumentoRepository, FakeStorage, make_pdf

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-rate-limit"
COMPETENCIA_VAZIA = "2099-12"
"""Competência que nenhum dado real usaria: o CSV sai praticamente vazio, e o
teste mede o freio, não o tamanho do extrato."""


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


def _criar_usuario(session: Session, papel: Papel = Papel.COORDENADOR) -> Usuario:
    """Coordenador por padrão — o papel que alcança as quatro rotas limitadas."""
    usuario = Usuario(
        nome="Pessoa de Teste - Rate Limit",
        email=f"ratelimit-{uuid.uuid4()}@teste.local",
        senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
        papel=papel.value,
        ativo=True,
    )
    session.add(usuario)
    session.commit()
    return usuario


def _limpar_usuario(session: Session, usuario: Usuario) -> None:
    """Apaga só o rastro deste usuário — inclusive os consumos da chave dele."""
    session.execute(
        text("delete from consumos_rate_limit where chave = :chave"),
        {"chave": f"{PREFIXO_USUARIO}{usuario.id}"},
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
def conferente(sessao: Session) -> Iterator[Usuario]:
    """Quem NÃO pode disparar a varredura de alertas — ela é de coordenador/gestor."""
    linha = _criar_usuario(sessao, Papel.CONFERENTE)
    yield linha
    _limpar_usuario(sessao, linha)


@pytest.fixture
def outro_usuario(sessao: Session) -> Iterator[Usuario]:
    """A segunda identidade: é ela que prova que os contadores são independentes."""
    linha = _criar_usuario(sessao)
    yield linha
    _limpar_usuario(sessao, linha)


def _aplicar_settings(settings: Settings, **limites: int) -> None:
    """Sobe a app com os limites do teste no lugar dos configurados.

    `environment="local"` é fixado pelo mesmo motivo de `tests/test_api_mfa.py`:
    fora de `local` o cookie de sessão sai com `Secure` e o `TestClient`, que
    fala HTTP, não o guardaria.
    """
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "environment": "local",
            "login_atraso_base_segundos": 0.0,
            "login_atraso_maximo_segundos": 0.0,
            **limites,
        }
    )


def _logar(usuario: Usuario) -> TestClient:
    cliente = TestClient(app)
    resposta = cliente.post(
        "/api/auth/login", json={"email": usuario.email, "senha": SENHA_DE_TESTE}
    )
    assert resposta.status_code == 200, resposta.text
    return cliente


def _csv(cliente: TestClient, **headers: str):  # type: ignore[no-untyped-def]
    return cliente.get(
        f"/api/relatorios/conferencia.csv?competencia={COMPETENCIA_VAZIA}",
        headers=headers or None,
    )


def _consumos(session: Session, chave: str, recurso: Recurso) -> list[ConsumoRateLimit]:
    return list(
        session.execute(
            select(ConsumoRateLimit)
            .where(ConsumoRateLimit.chave == chave, ConsumoRateLimit.recurso == recurso.value)
            .order_by(ConsumoRateLimit.created_at)
        )
        .scalars()
        .all()
    )


def _semear_consumos(
    session: Session, *, chave: str, recurso: Recurso, quantos: int, idade: timedelta
) -> None:
    """Grava consumos com `created_at` no passado — o relógio entra por dado."""
    agora = datetime.now(UTC)
    session.add_all(
        [
            ConsumoRateLimit(chave=chave, recurso=recurso.value, created_at=agora - idade)
            for _ in range(quantos)
        ]
    )
    session.commit()


@pytest.fixture(autouse=True)
def _limpar_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


# --- configuração --------------------------------------------------------------


def test_todo_recurso_tem_limite_configurado_e_o_da_maquina_e_mais_folgado() -> None:
    """AC3, metade estática: os quatro recursos têm limite, e o de máquina é maior.

    A tabela de `limites_do_recurso` é explícita justamente para um `Recurso`
    novo sem limite estourar em vez de silenciosamente virar `getattr` nenhum —
    este teste é onde isso estoura, e não em produção, na primeira requisição.

    O ADR determina que a chave de máquina receba limites próprios e **mais
    folgados**: o padrão de uso dela é legítimo e repetitivo.
    """
    padrao = Settings()

    for recurso in Recurso:
        limites = limites_do_recurso(padrao, recurso)
        assert limites.pessoa > 0, recurso
        assert limites.maquina >= limites.pessoa, recurso


# --- o freio ------------------------------------------------------------------


def test_dentro_do_limite_passa_e_ao_estourar_responde_429(
    settings: Settings, usuario: Usuario, sessao: Session
) -> None:
    """AC1 e AC6: dois passam, o terceiro é 429 — e o 429 diz qual recurso caiu."""
    _aplicar_settings(settings, limite_relatorio_csv_pessoa_por_hora=2)
    cliente = _logar(usuario)

    assert _csv(cliente).status_code == 200
    assert _csv(cliente).status_code == 200

    bloqueada = _csv(cliente)

    assert bloqueada.status_code == 429
    assert int(bloqueada.headers["retry-after"]) > 0
    corpo = bloqueada.json()
    assert corpo["error"]["tipo"] == "too_many_requests"
    assert Recurso.RELATORIO_CSV.rotulo in corpo["error"]["mensagem"]
    # A requisição bloqueada não vira consumo: 3 chamadas, 2 linhas.
    assert len(_consumos(sessao, f"{PREFIXO_USUARIO}{usuario.id}", Recurso.RELATORIO_CSV)) == 2


def test_retry_after_e_calculado_e_nao_a_janela_inteira(
    settings: Settings, usuario: Usuario, sessao: Session
) -> None:
    """AC1: o `Retry-After` é a janela menos a idade do consumo mais antigo.

    Com dois consumos de 50 minutos atrás e limite 2, a cota volta em ~10
    minutos — e não na hora cheia que um valor fixo reportaria. Um
    `Retry-After` inflado ensina a pessoa a ignorá-lo.
    """
    chave = f"{PREFIXO_USUARIO}{usuario.id}"
    _semear_consumos(
        sessao,
        chave=chave,
        recurso=Recurso.RELATORIO_CSV,
        quantos=2,
        idade=timedelta(minutes=50),
    )
    _aplicar_settings(settings, limite_relatorio_csv_pessoa_por_hora=2)
    cliente = _logar(usuario)

    bloqueada = _csv(cliente)

    assert bloqueada.status_code == 429
    restante = int(bloqueada.headers["retry-after"])
    assert 9 * 60 <= restante <= 10 * 60, restante
    assert restante < JANELA.total_seconds()


def test_pessoas_diferentes_nao_compartilham_cota(
    settings: Settings, usuario: Usuario, outro_usuario: Usuario
) -> None:
    """AC2 — o ponto do ADR: o contador é por identidade, não por origem.

    É exatamente o que um limite por IP não daria atrás do proxy do projeto:
    lá, a primeira pessoa a exportar dois relatórios travaria a equipe inteira.
    """
    _aplicar_settings(settings, limite_relatorio_csv_pessoa_por_hora=1)
    cliente = _logar(usuario)
    cliente_do_outro = _logar(outro_usuario)

    assert _csv(cliente).status_code == 200
    assert _csv(cliente).status_code == 429

    assert _csv(cliente_do_outro).status_code == 200


def test_chave_de_maquina_tem_contador_proprio_e_separado(
    settings: Settings, usuario: Usuario, sessao: Session
) -> None:
    """AC3: a `X-API-Key` conta em `maquina:api`, com o limite dela.

    A pessoa estoura no primeiro consumo; a máquina, com limite mais folgado,
    continua passando — são duas chaves diferentes na mesma tabela.
    """
    marco = datetime.now(UTC)
    _aplicar_settings(
        settings,
        limite_relatorio_csv_pessoa_por_hora=1,
        limite_relatorio_csv_maquina_por_hora=5,
    )
    cliente = _logar(usuario)

    assert _csv(cliente).status_code == 200
    assert _csv(cliente).status_code == 429

    maquina = TestClient(app)
    try:
        for _ in range(3):
            assert _csv(maquina, **AUTH_HEADERS).status_code == 200

        consumos_da_maquina = [
            consumo
            for consumo in _consumos(sessao, CHAVE_MAQUINA, Recurso.RELATORIO_CSV)
            if consumo.created_at >= marco
        ]
        assert len(consumos_da_maquina) == 3
        assert len(_consumos(sessao, f"{PREFIXO_USUARIO}{usuario.id}", Recurso.RELATORIO_CSV)) == 1
    finally:
        # Só as linhas que este teste criou: a chave de máquina é compartilhada
        # por qualquer outra suíte que use `X-API-Key`, então o recorte é por
        # chave **e** por instante de início — nunca um `delete` por chave só.
        sessao.execute(
            text(
                "delete from consumos_rate_limit "
                "where chave = :chave and recurso = :recurso and created_at >= :marco"
            ),
            {"chave": CHAVE_MAQUINA, "recurso": Recurso.RELATORIO_CSV.value, "marco": marco},
        )
        sessao.commit()


def test_passada_a_janela_a_cota_volta(
    settings: Settings, usuario: Usuario, sessao: Session
) -> None:
    """AC4: consumo mais velho que a janela não conta mais."""
    chave = f"{PREFIXO_USUARIO}{usuario.id}"
    _semear_consumos(
        sessao,
        chave=chave,
        recurso=Recurso.RELATORIO_CSV,
        quantos=3,
        idade=JANELA + timedelta(minutes=1),
    )
    _aplicar_settings(settings, limite_relatorio_csv_pessoa_por_hora=1)
    cliente = _logar(usuario)

    assert _csv(cliente).status_code == 200
    # O consumo de agora é o único dentro da janela — e agora ele fecha a cota.
    assert _csv(cliente).status_code == 429
    assert len(_consumos(sessao, chave, Recurso.RELATORIO_CSV)) == 4


def test_as_quatro_rotas_caras_sao_limitadas_e_a_rota_barata_nao(
    settings: Settings, usuario: Usuario
) -> None:
    """AC5: com todos os limites em zero, as quatro caem em 429 — e só elas.

    Limite zero é o jeito de exercitar as quatro rotas sem executar nenhuma:
    o 429 sai da dependency, antes do handler, então nada aqui chama o provider
    de IA, o storage ou o gateway de WhatsApp.

    `GET /api/operadoras` é a rota barata de amostra e responde 200 por mais
    que se bata nela, com os mesmos limites zerados: o ADR decidiu **não**
    cobrar de todo mundo o preço de proteger quatro rotas, e é esta asserção que
    trava a decisão.
    """
    _aplicar_settings(
        settings,
        limite_upload_documento_pessoa_por_hora=0,
        limite_relatorio_csv_pessoa_por_hora=0,
        limite_download_arquivo_pessoa_por_hora=0,
        limite_varredura_alertas_pessoa_por_hora=0,
    )
    cliente = _logar(usuario)

    respostas = {
        Recurso.UPLOAD_DOCUMENTO: cliente.post(
            "/api/documentos",
            files={"arquivo": ("evolucao.pdf", b"%PDF-nao-chega-a-ser-lido", "application/pdf")},
            data={"competencia": "2099-12"},
        ),
        Recurso.RELATORIO_CSV: _csv(cliente),
        Recurso.DOWNLOAD_ARQUIVO: cliente.get(f"/api/documentos/{uuid.uuid4()}/arquivo"),
        Recurso.VARREDURA_ALERTAS: cliente.post("/api/alertas/varredura"),
    }

    for recurso, resposta in respostas.items():
        assert resposta.status_code == 429, (recurso, resposta.text)
        assert int(resposta.headers["retry-after"]) > 0, recurso
        assert recurso.rotulo in resposta.json()["error"]["mensagem"], recurso

    for _ in range(15):
        assert cliente.get("/api/operadoras").status_code == 200


def test_upload_estourado_responde_429_sem_disparar_a_extracao(
    settings: Settings, usuario: Usuario
) -> None:
    """AC1 na rota que custa dinheiro: o trabalho caro não roda quando o limite cai.

    Repositório, storage e dispatcher são os dublês de `tests/fakes.py`, como em
    `tests/test_intake_router.py` — o provider de IA de verdade não é tocado em
    nenhum dos dois caminhos. O que este teste prova é a contagem: o dispatcher
    recebe a primeira página e **nada** na segunda requisição.
    """
    repositorio = FakeDocumentoRepository()
    storage = FakeStorage()
    dispatcher = FakeDispatcher()
    _aplicar_settings(settings, limite_upload_documento_pessoa_por_hora=1)
    app.dependency_overrides[get_documento_repository] = lambda: repositorio
    app.dependency_overrides[get_document_storage] = lambda: storage
    app.dependency_overrides[get_extraction_dispatcher] = lambda: dispatcher
    cliente = _logar(usuario)
    pdf = make_pdf(1)

    def _upload():  # type: ignore[no-untyped-def]
        return cliente.post(
            "/api/documentos",
            files={"arquivo": ("evolucao.pdf", pdf, "application/pdf")},
            data={"competencia": "2099-12"},
        )

    assert _upload().status_code == 201
    assert len(dispatcher.chamadas) == 1

    bloqueada = _upload()

    assert bloqueada.status_code == 429
    assert Recurso.UPLOAD_DOCUMENTO.rotulo in bloqueada.json()["error"]["mensagem"]
    assert len(dispatcher.chamadas) == 1, "a requisição bloqueada não pode chegar à extração"


def test_403_de_papel_nao_consome_cota(
    settings: Settings, conferente: Usuario, sessao: Session
) -> None:
    """Quem não pode entrar não gasta o limite de quem pode.

    A autorização por papel é aplicada no `include_router` e é avaliada antes da
    dependency do freio. Se a ordem se invertesse, uma tela chamando a rota
    errada esvaziaria a cota de uma pessoa que sequer tem acesso a ela — e o
    sintoma apareceria depois, na rota certa.
    """
    _aplicar_settings(settings, limite_varredura_alertas_pessoa_por_hora=1)
    cliente = _logar(conferente)

    assert cliente.post("/api/alertas/varredura").status_code == 403

    assert _consumos(sessao, f"{PREFIXO_USUARIO}{conferente.id}", Recurso.VARREDURA_ALERTAS) == []


# --- o que a tabela guarda ------------------------------------------------------


def test_o_contador_nao_guarda_credencial_nem_dado_pessoal(
    settings: Settings, usuario: Usuario, sessao: Session
) -> None:
    """AC7: a chave é o id da identidade, e a tabela não tem mais nada.

    Nem e-mail, nem token de sessão, nem a `X-API-Key`: o contador só precisa
    saber quantas vezes uma identidade consumiu um recurso, e o id opaco basta.
    A asserção sobre as colunas é o que impede alguém de acrescentar um campo
    "para facilitar a depuração" e trazer dado pessoal junto.
    """
    _aplicar_settings(settings, limite_relatorio_csv_pessoa_por_hora=5)
    cliente = _logar(usuario)

    assert _csv(cliente).status_code == 200

    consumos = _consumos(sessao, f"{PREFIXO_USUARIO}{usuario.id}", Recurso.RELATORIO_CSV)
    assert len(consumos) == 1
    assert consumos[0].chave == f"usuario:{usuario.id}"
    assert usuario.email not in consumos[0].chave

    colunas = {
        coluna["name"] for coluna in inspect(sessao.get_bind()).get_columns("consumos_rate_limit")
    }
    assert colunas == {"id", "chave", "recurso", "created_at"}
