"""Testes de integração da configuração de canais em banco (ADR 0006, parte 2) —
contra Postgres real (localhost:5434).

O que estes testes guardam é o desenho do ADR, e não "a rota funciona":

1. o estado inicial existe e espelha o comportamento anterior — tabela vazia
   significaria a operação em silêncio a partir do deploy;
2. desligar um canal pela API muda a varredura **na requisição seguinte**, sem
   reiniciar nada;
3. `habilitado` e `disponivel` continuam sendo perguntas separadas;
4. quem escreve é o coordenador, quem lê é ele e o gestor;
5. toda mudança de estado é auditada, e o que não muda não gera evento;
6. a chave de máquina é auditada com ator nulo e rótulo `"api"`.

## O teardown é o ponto mais delicado deste arquivo

`canais_alerta` é **global e minúscula**: duas linhas, uma por canal, lidas por
toda varredura do processo. Um teste que desligue um canal e não o restaure
envenena todos os seguintes — inclusive os de `tests/test_api_alertas.py`, que
esperam o WhatsApp ligado, e a falha apareceria no módulo errado. A estratégia
é a mesma disciplina do resto da suíte ("apaga só o que o teste criou, nunca
`TRUNCATE`"), adaptada a uma tabela que não se apaga:

- **snapshot e restauração.** `canais_restaurados` fotografa as quatro colunas
  mutáveis de cada linha antes do teste e as escreve de volta depois, o que
  devolve a tabela ao byte anterior mesmo quando o teste falha no meio.
- **a auditoria é apagada por ator e por marco de tempo.** Os eventos de pessoa
  saem pelo `usuario_id` dos usuários do teste; os da `X-API-Key` têm
  `usuario_id` nulo e saem pelo `created_at >= marco`, com o marco lido do
  **relógio do Postgres** (e não do processo de teste, que pode estar
  dessincronizado do container).
- **a ordem importa por causa da FK.** `canais_alerta.atualizado_por_usuario_id`
  referencia `usuarios`, então a restauração precisa acontecer **antes** de o
  teardown de `usuarios` apagar as contas. É por isso que `canais_restaurados`
  depende de `usuarios`: o pytest finaliza na ordem inversa do setup.

## O estado de partida também é escrito, e não presumido

O teardown resolvia metade do problema: o teste devolvia a tabela ao que ela
era, mas continuava **presumindo** de onde partia. Num banco recém-semeado o
e-mail nasce desligado, e ligá-lo era uma mudança de verdade; num banco onde
alguém ligou o canal pela tela, o mesmo `PATCH` vira **no-op** — e no-op é
comportamento correto e documentado (`canais_repository.definir_habilitado`
não gera evento nem recarimba quando o valor enviado já é o atual). O carimbo e
o evento que o teste esperava simplesmente não existem, e a suíte reprova por
causa do histórico do banco, não do código.

Por isso `estado_inicial_dos_canais` escreve a linha de partida antes do teste
agir, devolvendo-a ao estado que a migration semeia (`atualizado_*` nulos): o
`PATCH` sob teste volta a ser uma transição de verdade em qualquer máquina.
Ele depende de `canais_restaurados`, então a fotografia da restauração é
sempre do estado **anterior** ao teste — o banco de desenvolvimento continua
saindo daqui como entrou.

O padrão da issue #47 (`e325814`: a fixture cria a própria entidade com
identificador único e o teste filtra por ela) não serve aqui — `canais_alerta`
tem duas linhas fixas, uma por canal do enum, e não há entidade exclusiva a
criar. A forma mais próxima em espírito é esta: em vez de possuir a entidade, o
teste possui o estado dela durante a sua execução.

As asserções sobre a auditoria já eram recorte fechado e continuam como
estavam: todas filtram pelo `usuario_id` do coordenador do próprio teste (um
usuário novo a cada execução), então evento de terceiro nunca entra na conta.

Nenhuma mensagem sai daqui: não há credencial de uazapi nem de SMTP, e os
testes que exercitam a varredura o fazem sem destinatário configurado.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from homecareos.alerts import canais_repository
from homecareos.alerts.canais import construir_canais
from homecareos.alerts.schema import Canal
from homecareos.auth import senhas
from homecareos.auth.dependencies import MENSAGEM_SEM_PERMISSAO
from homecareos.auth.schema import ROTULO_MAQUINA, Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import AuditoriaCanal, ConfiguracaoCanal, Usuario
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from homecareos.seed import seed_canais
from tests.conftest import AUTH_HEADERS, TEST_API_KEY, TEST_API_KEY_PAPEIS

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-canais"

BASE_URL_FALSA = "https://instancia-de-teste.uazapi.com"
TOKEN_FALSO = "token-que-nunca-sai-daqui"


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
def usuarios(sessao: Session) -> Iterator[dict[Papel, Usuario]]:
    """Um usuário de cada papel, com e-mail único (o banco é compartilhado)."""
    criados = {
        papel: Usuario(
            nome=f"Pessoa Canais {papel.value}",
            email=f"canais-{papel.value}-{uuid.uuid4()}@teste.local",
            senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
            papel=papel.value,
        )
        for papel in Papel
    }
    sessao.add_all(list(criados.values()))
    sessao.commit()

    yield criados

    ids = [usuario.id for usuario in criados.values()]
    sessao.execute(text("delete from sessoes where usuario_id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from usuarios where id = any(:ids)"), {"ids": ids})
    sessao.commit()


@pytest.fixture
def canais_restaurados(sessao: Session, usuarios: dict[Papel, Usuario]) -> Iterator[None]:
    """Devolve `canais_alerta` ao estado anterior e apaga a auditoria do teste.

    Depende de `usuarios` de propósito: assim o teardown daqui roda **antes** do
    teardown deles, e a FK `atualizado_por_usuario_id` já está nula (ou de volta
    ao valor original) quando as contas somem. Ver a docstring do módulo.
    """
    marco = sessao.execute(select(func.now())).scalar_one()
    antes = [
        {
            "id": linha.id,
            "habilitado": linha.habilitado,
            "atualizado_em": linha.atualizado_em,
            "atualizado_por": linha.atualizado_por,
            "atualizado_por_usuario_id": linha.atualizado_por_usuario_id,
        }
        for linha in sessao.scalars(select(ConfiguracaoCanal)).all()
    ]

    yield

    ids = [usuario.id for usuario in usuarios.values()]
    sessao.execute(
        text(
            "delete from auditoria_canais_alerta "
            "where usuario_id = any(:ids) or created_at >= :marco"
        ),
        {"ids": ids, "marco": marco},
    )
    for original in antes:
        sessao.execute(
            text(
                "update canais_alerta set habilitado = :habilitado, "
                "atualizado_em = :atualizado_em, atualizado_por = :atualizado_por, "
                "atualizado_por_usuario_id = :atualizado_por_usuario_id where id = :id"
            ),
            original,
        )
    sessao.commit()


@pytest.fixture
def estado_inicial_dos_canais(
    sessao: Session, canais_restaurados: None
) -> Callable[[dict[Canal, bool]], None]:
    """Escreve o estado de partida do teste, em vez de presumi-lo. Ver o módulo.

    Devolve cada linha pedida ao estado que a migration semeia: o `habilitado`
    escolhido e o trio `atualizado_em`/`atualizado_por`/`atualizado_por_usuario_id`
    nulo — "ninguém decidiu nada ainda", que é o que
    `test_carimbo_e_ator_do_estado_atual_andam_sempre_juntos` descreve como o
    estado semeado. Sem zerar o carimbo, um teste sobre autoria poderia passar
    lendo a decisão de outra pessoa.

    Escreve por SQL cru e commita porque quem lê depois é o endpoint, na sessão
    dele: sem o commit a mudança ficaria presa nesta transação e a requisição
    seguinte continuaria vendo o estado antigo.

    Depende de `canais_restaurados` para garantir a ordem: a fotografia da
    restauração é tirada no setup dele, portanto **antes** desta escrita.
    """

    def definir(estado: dict[Canal, bool]) -> None:
        for canal, habilitado in estado.items():
            sessao.execute(
                text(
                    "update canais_alerta set habilitado = :habilitado, "
                    "atualizado_em = null, atualizado_por = null, "
                    "atualizado_por_usuario_id = null where canal = :canal"
                ),
                {"habilitado": habilitado, "canal": canal.value},
            )
        sessao.commit()

    return definir


def _overrides(settings: Settings, **extra: object) -> Settings:
    base: dict[str, object] = {
        "api_keys": TEST_API_KEY,
        "api_key_papeis": TEST_API_KEY_PAPEIS,
        # Fora de `local` o cookie sai `Secure`, o `TestClient` fala HTTP e o
        # cookie não seria guardado — os testes de papel passariam a medir a
        # flag em vez da autorização.
        "environment": "local",
        "login_atraso_base_segundos": 0.0,
        "login_atraso_maximo_segundos": 0.0,
        # Sem destinatário nenhum: os testes que rodam a varredura de verdade
        # exercitam o estado dos canais, e nenhuma mensagem pode sair daqui.
        "alertas_destinatarios": "",
    }
    return settings.model_copy(update=base | extra)


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """Cliente sem sessão, usado com `X-API-Key`."""
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


def _por_canal(corpo: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["canal"]): item for item in corpo}


# --- o estado inicial: a migration semeou, e a tabela não nasceu vazia ----------


def test_a_tabela_tem_uma_linha_por_canal_conhecido(sessao: Session) -> None:
    """Tabela vazia significaria **nenhum canal envia** — a operação em silêncio
    a partir do deploy, sem erro e sem aviso. É por isso que a migration insere
    as linhas em vez de deixar o caso para o `seed.py` (ver a docstring de
    `a4d6c8b21f37`)."""
    estado = canais_repository.listar_estado(sessao)

    assert set(estado) == set(Canal)


def test_carimbo_e_ator_do_estado_atual_andam_sempre_juntos(sessao: Session) -> None:
    """ "Quem decidiu" e "quando" são uma informação só, e nenhuma linha pode ter
    metade dela.

    No estado semeado pela migration os dois são nulos, e é a resposta honesta:
    ninguém decidiu nada, o valor foi herdado de `ALERTAS_CANAIS`. Preencher
    `atualizado_por` com um ator fictício ("migration", "sistema") faria a tela
    mentir sobre uma decisão que pessoa nenhuma tomou. Depois de um `PATCH` os
    dois estão preenchidos — ver
    `test_o_patch_carimba_quem_decidiu_e_quando`.
    """
    linhas = sessao.scalars(select(ConfiguracaoCanal)).all()

    assert linhas
    for linha in linhas:
        assert (linha.atualizado_em is None) == (linha.atualizado_por is None)


def test_o_patch_carimba_quem_decidiu_e_quando(
    sessao: Session,
    usuarios: dict[Papel, Usuario],
    clientes: dict[Papel, TestClient],
    estado_inicial_dos_canais: Callable[[dict[Canal, bool]], None],
) -> None:
    """Saber que o canal está desligado sem saber desde quando não ajuda numa
    investigação — é o par que evita ter de paginar a auditoria para a pergunta
    mais comum."""
    estado_inicial_dos_canais({Canal.EMAIL: False})
    coordenador = usuarios[Papel.COORDENADOR]

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/alertas/canais/{Canal.EMAIL.value}", json={"habilitado": True}
    )
    sessao.commit()

    assert resposta.json()["atualizado_por"] == coordenador.email
    assert resposta.json()["atualizado_em"] is not None
    linha = sessao.scalars(
        select(ConfiguracaoCanal).where(ConfiguracaoCanal.canal == Canal.EMAIL.value)
    ).one()
    assert linha.atualizado_por == coordenador.email
    assert linha.atualizado_por_usuario_id == coordenador.id
    assert linha.atualizado_em is not None


# --- a leitura: habilitado e disponível são perguntas separadas -----------------


def test_a_leitura_devolve_todos_os_canais_com_os_dois_estados(
    clientes: dict[Papel, TestClient],
) -> None:
    resposta = clientes[Papel.COORDENADOR].get("/api/alertas/canais")

    assert resposta.status_code == 200
    corpo = _por_canal(resposta.json())
    assert set(corpo) == {canal.value for canal in Canal}
    for item in corpo.values():
        assert set(item) == {
            "canal",
            "habilitado",
            "disponivel",
            "atualizado_em",
            "atualizado_por",
        }


def test_canal_ligado_sem_credencial_mostra_habilitado_e_disponivel_distintos(
    settings: Settings, clientes: dict[Papel, TestClient], canais_restaurados: None
) -> None:
    """Critério 3 do handoff. "Desliguei" e "esqueci a credencial" precisam ser
    distinguíveis, senão quem liga o canal na tela não entende por que nada sai.

    Aqui o WhatsApp tem credencial e o e-mail não; os dois são ligados pela API.
    """
    app.dependency_overrides[get_settings] = lambda: _overrides(
        settings, uazapi_base_url=BASE_URL_FALSA, uazapi_token=TOKEN_FALSO
    )
    coordenador = clientes[Papel.COORDENADOR]

    for canal in Canal:
        assert (
            coordenador.patch(f"/api/alertas/canais/{canal.value}", json={"habilitado": True})
        ).status_code == 200

    corpo = _por_canal(coordenador.get("/api/alertas/canais").json())

    assert corpo[Canal.WHATSAPP.value]["habilitado"] is True
    assert corpo[Canal.WHATSAPP.value]["disponivel"] is True
    assert corpo[Canal.EMAIL.value]["habilitado"] is True
    assert corpo[Canal.EMAIL.value]["disponivel"] is False


def test_ligar_canal_sem_credencial_nao_envia_e_nao_estoura(
    settings: Settings, clientes: dict[Papel, TestClient], canais_restaurados: None
) -> None:
    """A outra metade do critério 3: a varredura roda inteira com um canal ligado
    e sem credencial, e nada sai nem quebra."""
    app.dependency_overrides[get_settings] = lambda: _overrides(settings)
    coordenador = clientes[Papel.COORDENADOR]
    coordenador.patch(f"/api/alertas/canais/{Canal.EMAIL.value}", json={"habilitado": True})

    resposta = coordenador.post("/api/alertas/varredura")

    assert resposta.status_code == 200
    resumo = resposta.json()
    assert resumo["enviados"] == 0
    assert resumo["falhas"] == 0
    assert resumo["canais"][Canal.EMAIL.value] == {"habilitado": True, "disponivel": False}


# --- a troca de fonte: o banco decide, e decide na requisição seguinte ----------


def test_desligar_o_canal_pela_api_muda_a_varredura_sem_reiniciar_nada(
    settings: Settings, clientes: dict[Papel, TestClient], canais_restaurados: None
) -> None:
    """**Critério 2 do handoff.** É esta a prova de que a fonte mudou de lugar:
    o mesmo processo, o mesmo `TestClient`, duas varreduras e um `PATCH` no
    meio. Enquanto o liga/desliga era `ALERTAS_CANAIS`, mudá-lo exigia acesso ao
    servidor e um deploy.
    """
    app.dependency_overrides[get_settings] = lambda: _overrides(
        settings, uazapi_base_url=BASE_URL_FALSA, uazapi_token=TOKEN_FALSO
    )
    coordenador = clientes[Papel.COORDENADOR]
    coordenador.patch(f"/api/alertas/canais/{Canal.WHATSAPP.value}", json={"habilitado": True})

    antes = coordenador.post("/api/alertas/varredura").json()
    assert antes["canais"][Canal.WHATSAPP.value]["habilitado"] is True
    assert antes["provider_configurado"] is True

    coordenador.patch(f"/api/alertas/canais/{Canal.WHATSAPP.value}", json={"habilitado": False})

    depois = coordenador.post("/api/alertas/varredura").json()
    assert depois["canais"][Canal.WHATSAPP.value]["habilitado"] is False
    # `provider_configurado` é "o WhatsApp está ligado E com credencial": a
    # credencial não mudou, o liga/desliga mudou.
    assert depois["provider_configurado"] is False


def test_construir_canais_le_o_estado_do_banco(
    sessao: Session, settings: Settings, clientes: dict[Papel, TestClient], canais_restaurados: None
) -> None:
    """O mesmo, um nível abaixo do endpoint: é `construir_canais` que passou a
    consultar `canais_alerta`, e é dele que o cron e o gancho dependem."""
    clientes[Papel.COORDENADOR].patch(
        f"/api/alertas/canais/{Canal.EMAIL.value}", json={"habilitado": True}
    )
    sessao.commit()  # a sessão do teste precisa enxergar o commit do endpoint

    por_canal = {canal.canal: canal for canal in construir_canais(sessao, settings)}

    assert por_canal[Canal.EMAIL].habilitado is True


# --- a matriz de papéis ---------------------------------------------------------


def test_coordenador_altera_e_gestor_le_mas_nao_altera(
    clientes: dict[Papel, TestClient], canais_restaurados: None
) -> None:
    """**Critério 4 do handoff.** Ligar e desligar canal é operação, e quem opera
    é o coordenador (ADR 0006, que manteve a matriz do ADR 0001 intacta: o gestor
    lê a operação inteira, não a executa)."""
    alvo = f"/api/alertas/canais/{Canal.EMAIL.value}"

    assert clientes[Papel.COORDENADOR].patch(alvo, json={"habilitado": True}).status_code == 200
    assert clientes[Papel.GESTOR].get("/api/alertas/canais").status_code == 200

    recusa = clientes[Papel.GESTOR].patch(alvo, json={"habilitado": False})
    assert recusa.status_code == 403
    assert recusa.json()["error"]["mensagem"] == MENSAGEM_SEM_PERMISSAO


def test_conferente_nao_le_nem_altera(clientes: dict[Papel, TestClient]) -> None:
    conferente = clientes[Papel.CONFERENTE]

    assert conferente.get("/api/alertas/canais").status_code == 403
    assert (
        conferente.patch(
            f"/api/alertas/canais/{Canal.EMAIL.value}", json={"habilitado": True}
        ).status_code
        == 403
    )


def test_gestor_le_a_auditoria_dos_canais(clientes: dict[Papel, TestClient]) -> None:
    """ "Por que ninguém foi avisado?" é pergunta de quem acompanha a operação, e
    o gestor já lê o log de `/api/alertas`, que expõe mais do que esta rota."""
    assert clientes[Papel.GESTOR].get("/api/alertas/canais/auditoria").status_code == 200
    assert clientes[Papel.CONFERENTE].get("/api/alertas/canais/auditoria").status_code == 403


def test_canal_desconhecido_no_path_responde_422(clientes: dict[Papel, TestClient]) -> None:
    """O path param é `alerts.schema.Canal`: o FastAPI recusa antes do handler,
    e nenhuma linha é criada para um canal que o sistema não implementa."""
    resposta = clientes[Papel.COORDENADOR].patch(
        "/api/alertas/canais/telegrama", json={"habilitado": True}
    )

    assert resposta.status_code == 422


# --- a auditoria ----------------------------------------------------------------


def test_toda_mudanca_gera_evento_com_ator_canal_de_para_e_quando(
    sessao: Session,
    usuarios: dict[Papel, Usuario],
    clientes: dict[Papel, TestClient],
    estado_inicial_dos_canais: Callable[[dict[Canal, bool]], None],
) -> None:
    """**Critério 5 do handoff.** Quem desliga um canal silencia a operação, e é
    isso que torna a auditoria obrigatória: sem ela, "por que ninguém foi
    avisado?" é uma pergunta sem resposta possível."""
    # Desligado é o estado de partida que os dois eventos esperados exigem: com
    # o e-mail já ligado, o primeiro `PATCH` seria no-op e sobraria um evento só.
    estado_inicial_dos_canais({Canal.EMAIL: False})
    coordenador = usuarios[Papel.COORDENADOR]
    alvo = f"/api/alertas/canais/{Canal.EMAIL.value}"

    clientes[Papel.COORDENADOR].patch(alvo, json={"habilitado": True})
    clientes[Papel.COORDENADOR].patch(alvo, json={"habilitado": False})
    sessao.commit()

    eventos = sessao.scalars(
        select(AuditoriaCanal)
        .where(AuditoriaCanal.usuario_id == coordenador.id)
        .order_by(AuditoriaCanal.created_at)
    ).all()

    assert [(evento.habilitado_de, evento.habilitado_para) for evento in eventos] == [
        (False, True),
        (True, False),
    ]
    for evento in eventos:
        assert evento.usuario == coordenador.email
        assert evento.canal == Canal.EMAIL.value
        assert isinstance(evento.created_at, datetime)


def test_mudanca_que_nao_muda_nada_nao_gera_evento(
    sessao: Session,
    usuarios: dict[Papel, Usuario],
    clientes: dict[Papel, TestClient],
    estado_inicial_dos_canais: Callable[[dict[Canal, bool]], None],
) -> None:
    """Ligar um canal já ligado não é evento — e não pode reescrever
    `atualizado_por`, que responde "quem decidiu o estado atual" e não "quem
    clicou por último"."""
    # O no-op só é observável depois de uma mudança de verdade: é ela que produz
    # o carimbo e o evento únicos que a segunda chamada não pode mexer.
    estado_inicial_dos_canais({Canal.EMAIL: False})
    coordenador = usuarios[Papel.COORDENADOR]
    alvo = f"/api/alertas/canais/{Canal.EMAIL.value}"
    clientes[Papel.COORDENADOR].patch(alvo, json={"habilitado": True})
    sessao.commit()
    linha = sessao.scalars(
        select(ConfiguracaoCanal).where(ConfiguracaoCanal.canal == Canal.EMAIL.value)
    ).one()
    carimbo_da_decisao = linha.atualizado_em
    assert carimbo_da_decisao is not None

    resposta = clientes[Papel.COORDENADOR].patch(alvo, json={"habilitado": True})
    sessao.commit()
    sessao.expire_all()

    assert resposta.status_code == 200
    assert resposta.json()["habilitado"] is True
    total = sessao.execute(
        select(func.count())
        .select_from(AuditoriaCanal)
        .where(AuditoriaCanal.usuario_id == coordenador.id)
    ).scalar_one()
    assert total == 1
    linha_depois = sessao.scalars(
        select(ConfiguracaoCanal).where(ConfiguracaoCanal.canal == Canal.EMAIL.value)
    ).one()
    assert linha_depois.atualizado_em == carimbo_da_decisao


def test_chamada_por_api_key_e_auditada_com_ator_nulo_e_rotulo_api(
    sessao: Session,
    api: TestClient,
    estado_inicial_dos_canais: Callable[[dict[Canal, bool]], None],
) -> None:
    """**Critério 6 do handoff.** `exigir_papel` deixa a chave passar em qualquer
    papel, então uma chamada de máquina pode mudar canal. Forjar um id de usuário
    nesse caso apontaria a auditoria para quem não agiu.

    O ator da chave é nulo, então este é o único teste do arquivo cuja busca não
    pode filtrar pelo `usuario_id` do próprio teste — ele lê o evento mais
    recente de ator nulo. Sem o estado de partida escrito, num banco onde o
    e-mail já estivesse ligado o `PATCH` seria no-op e a asserção passaria lendo
    um evento que **outra** chamada de máquina gravou.
    """
    estado_inicial_dos_canais({Canal.EMAIL: False})

    resposta = api.patch(
        f"/api/alertas/canais/{Canal.EMAIL.value}", json={"habilitado": True}, headers=AUTH_HEADERS
    )
    sessao.commit()

    assert resposta.status_code == 200
    evento = sessao.scalars(
        select(AuditoriaCanal)
        .where(AuditoriaCanal.usuario_id.is_(None), AuditoriaCanal.canal == Canal.EMAIL.value)
        .order_by(AuditoriaCanal.created_at.desc())
    ).first()
    assert evento is not None
    assert evento.usuario == ROTULO_MAQUINA
    assert evento.habilitado_para is True

    linha = sessao.scalars(
        select(ConfiguracaoCanal).where(ConfiguracaoCanal.canal == Canal.EMAIL.value)
    ).one()
    assert linha.atualizado_por == ROTULO_MAQUINA
    assert linha.atualizado_por_usuario_id is None


def test_a_leitura_da_auditoria_filtra_por_ator_canal_e_estado(
    usuarios: dict[Papel, Usuario],
    clientes: dict[Papel, TestClient],
    estado_inicial_dos_canais: Callable[[dict[Canal, bool]], None],
) -> None:
    """Os três filtros da rota, e o mais importante deles: `habilitado=false`
    responde "quem silenciou a operação?"."""
    # Os três `PATCH` abaixo precisam ser três mudanças de verdade para virarem
    # os três eventos que os filtros recortam: e-mail desligado (para poder
    # ligar e desligar) e WhatsApp ligado (para poder desligar).
    estado_inicial_dos_canais({Canal.EMAIL: False, Canal.WHATSAPP: True})
    coordenador = usuarios[Papel.COORDENADOR]
    cliente = clientes[Papel.COORDENADOR]
    cliente.patch(f"/api/alertas/canais/{Canal.EMAIL.value}", json={"habilitado": True})
    cliente.patch(f"/api/alertas/canais/{Canal.EMAIL.value}", json={"habilitado": False})
    cliente.patch(f"/api/alertas/canais/{Canal.WHATSAPP.value}", json={"habilitado": False})

    do_ator = cliente.get(f"/api/alertas/canais/auditoria?ator_id={coordenador.id}").json()
    desligamentos = cliente.get(
        f"/api/alertas/canais/auditoria?ator_id={coordenador.id}&habilitado=false"
    ).json()
    do_email = cliente.get(
        f"/api/alertas/canais/auditoria?ator_id={coordenador.id}&canal={Canal.EMAIL.value}"
    ).json()

    assert do_ator["paginacao"]["total"] == 3
    # Do mais recente para o mais antigo, como a auditoria de usuários.
    assert do_ator["data"][0]["canal"] == Canal.WHATSAPP.value
    assert desligamentos["paginacao"]["total"] == 2
    assert {item["canal"] for item in desligamentos["data"]} == {
        Canal.EMAIL.value,
        Canal.WHATSAPP.value,
    }
    assert do_email["paginacao"]["total"] == 2


# --- o seed é rede, não fonte ---------------------------------------------------


def test_o_seed_nao_duplica_linha_nem_desfaz_decisao_de_quem_mexeu_na_tela(
    sessao: Session, clientes: dict[Papel, TestClient], canais_restaurados: None
) -> None:
    """`seed_canais` roda em todo deploy e cobre o canal que nascer depois da
    migration. Se ele reescrevesse o estado, um deploy desligaria em silêncio o
    canal que a operação acabou de ligar — o mesmo desastre, por outra porta.
    """
    clientes[Papel.COORDENADOR].patch(
        f"/api/alertas/canais/{Canal.EMAIL.value}", json={"habilitado": True}
    )

    seed_canais()
    seed_canais()

    sessao.commit()
    sessao.expire_all()
    linhas = sessao.scalars(
        select(ConfiguracaoCanal).where(ConfiguracaoCanal.canal == Canal.EMAIL.value)
    ).all()
    assert len(linhas) == 1
    assert linhas[0].habilitado is True
