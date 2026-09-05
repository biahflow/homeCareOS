"""Testes de integração da trilha de alertas — contra Postgres real (localhost:5434).

**Nenhuma requisição de rede.** O gateway é sempre um dublê em memória
(`ProviderFake`), injetado por `app.dependency_overrides` no endpoint e por
parâmetro nas chamadas diretas de serviço. Não existe credencial de uazapi aqui.

O banco é compartilhado com o desenvolvimento e já tem operadoras seedadas, o
catálogo de regras e a tabela de baseline. Duas consequências que moldam o
arquivo inteiro:

- o cenário cria **sempre** a própria operadora (`ALERT-<uuid>`) e usa
  competências que ninguém mais usa (`2097-06`/`2097-07`/`2097-08`), e toda
  asserção filtra pelos próprios registros;
- os detectores varrem a base **inteira**, então os testes de detecção chamam o
  detector e filtram o resultado pelos próprios documentos, e os testes de
  despacho passam uma lista de alertas montada à mão. Rodar
  `executar_varredura` e contar o que o dublê recebeu contaria também o que
  outras trilhas deixaram no banco.

O teardown apaga tudo o que o teste criou, inclusive as linhas de
`alertas_enviados` — inclusive as que o gancho da classificação grava.

## Por que o canal de e-mail não despacha por papel aqui (ADR 0006)

O canal de e-mail resolve destinatário pelos **usuários ativos** de um papel, e
a base é compartilhada: outras trilhas deixam coordenador e gestor ativos que
este módulo não criou. Despachar por papel de verdade aqui teria dois efeitos
ruins, e o segundo é pior que o primeiro:

1. a contagem de envios deixaria de ser determinística;
2. as linhas gravadas apontariam (`alertas_enviados.usuario_id`) para usuários
   de OUTROS módulos, e o `delete from usuarios` do teardown deles quebraria
   por violação de FK — uma falha que apareceria no módulo errado.

Por isso a divisão: a **resolução por papel** é exercitada contra o banco de
verdade em `test_email_por_papel_*` (só leitura, nenhuma linha escrita), e o
**despacho** usa o `CanalEmail` real com o destinatário fixado pelo teste
(`CanalEmailComDestinatarioFixo`) — template, entrega e tradução de erro
continuam sendo os de produção.

**Nenhum e-mail e nenhuma mensagem de WhatsApp são enviados de verdade**: os
dois gateways são dublês em memória, e não há credencial de uazapi nem de SMTP
neste arquivo.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from homecareos.alerts import hooks, repository, scan
from homecareos.alerts.canais import CanalAlerta, CanalEmail, CanalWhatsApp
from homecareos.alerts.detectores import (
    detectar_deadline_competencia,
    detectar_documento_incompleto_critico,
    detectar_pendencia_parada,
    detectar_volume_anormal,
)
from homecareos.alerts.errors import EnvioError
from homecareos.alerts.router import obter_canais
from homecareos.alerts.schema import (
    Alerta,
    Canal,
    Destinatario,
    MensagemAlerta,
    StatusAlerta,
    TipoAlerta,
)
from homecareos.alerts.service import despachar
from homecareos.auth import senhas
from homecareos.auth.schema import Papel
from homecareos.classification.service import classificar_documento
from homecareos.config import Settings, get_settings
from homecareos.db.models import (
    AlertaEnviado,
    Documento,
    DocumentoStatus,
    Modalidade,
    Operadora,
    Paciente,
    Pendencia,
    PendenciaStatus,
    TipoDocumento,
    Usuario,
)
from homecareos.db.models.enums import ResultadoValidacao
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from homecareos.rules.schema import AcaoRegra, ResultadoAvaliacao
from tests.conftest import AUTH_HEADERS, TEST_API_KEY, TEST_API_KEY_PAPEIS

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2

COMPETENCIA = "2097-06"
COMPETENCIA_DEADLINE = "2097-07"
COMPETENCIA_FORA_DA_JANELA = "2097-08"

BASE_URL_FALSA = "https://instancia-de-teste.uazapi.com"
TOKEN_FALSO = "token-que-nunca-sai-daqui"


def _numero() -> str:
    """Telefone único deste processo de teste, para o teardown achar as linhas."""
    return f"5521{uuid.uuid4().int % 10**9:09d}"


DESTINATARIO_A = _numero()
DESTINATARIO_B = _numero()
NUMEROS_DO_TESTE = (DESTINATARIO_A, DESTINATARIO_B)

MARCA = uuid.uuid4().hex[:12]
"""Marca deste processo de teste, embutida na `chave` dos alertas que os testes
de canal criam. É o que permite ao teardown apagar as linhas por assunto, e não
só por destinatário — necessário desde que o e-mail entrou, porque o endereço
deixou de ser sempre um dos dois telefones conhecidos."""

PREFIXO_CHAVE = f"documento:{MARCA}:"


def _chave(nome: str) -> str:
    return f"{PREFIXO_CHAVE}{nome}"


class ProviderFake:
    """Gateway em memória: acumula o que recebeu e, se pedido, recusa o envio."""

    def __init__(self, falhar_para: set[str] | None = None) -> None:
        self.enviadas: list[tuple[str, str]] = []
        self.falhar_para = falhar_para or set()

    def enviar(self, destinatario: str, mensagem: str) -> None:
        if destinatario in self.falhar_para:
            raise EnvioError("gateway de WhatsApp recusou o envio: HTTP 401 Invalid token.")
        self.enviadas.append((destinatario, mensagem))


class ProviderEmailFake:
    """Caixa postal em memória. **Nenhuma conexão SMTP é aberta.**"""

    def __init__(self) -> None:
        self.enviadas: list[tuple[str, str, str]] = []

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        self.enviadas.append((destinatario, assunto, corpo))


class CanalEmailComDestinatarioFixo(CanalEmail):
    """`CanalEmail` de verdade com o destinatário fixado pelo teste.

    Template, entrega e tradução de erro continuam sendo os de produção; só a
    resolução por papel é substituída, e pela razão de isolamento explicada na
    docstring do módulo. A resolução real é exercitada em
    `test_email_por_papel_*`.
    """

    def __init__(self, *, provider: ProviderEmailFake, destinatarios: list[Destinatario]) -> None:
        super().__init__(habilitado=True, provider=provider)
        self._fixos = destinatarios

    def destinatarios(
        self, session: Session, settings: Settings, tipo: TipoAlerta
    ) -> list[Destinatario]:
        return list(self._fixos)


def _canais(
    provider: ProviderFake | None = None,
    *,
    email: CanalEmail | None = None,
    whatsapp_habilitado: bool = True,
) -> list[CanalAlerta]:
    """Os canais que os testes de despacho injetam no lugar de `construir_canais`.

    Devolve **sempre os dois**, como `construir_canais` faz em produção: um
    canal ausente da lista sumiria do resumo, e o resumo tem de responder por
    todos. Sem `email=`, o de e-mail entra no estado de produção padrão —
    desligado por configuração e sem credencial —, que é o que a maioria destes
    testes exercita.
    """
    canais: list[CanalAlerta] = [CanalWhatsApp(habilitado=whatsapp_habilitado, provider=provider)]
    canais.append(email if email is not None else CanalEmail(habilitado=False, provider=None))
    return canais


class ProviderQueExplode:
    """Gateway que levanta erro **não** previsto pelo serviço.

    `RuntimeError`, e não `EnvioError`, de propósito: `EnvioError` o serviço
    já trata: o que este dublê testa é a blindagem externa do gancho.
    """

    def enviar(self, destinatario: str, mensagem: str) -> None:
        raise RuntimeError("o gateway explodiu de um jeito que ninguém previu")


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


def _com_alertas(settings: Settings, **overrides: object) -> Settings:
    """Settings do cenário: gateway "configurado" e destinatários próprios do teste."""
    base: dict[str, object] = {
        "api_keys": TEST_API_KEY,
        "api_key_papeis": TEST_API_KEY_PAPEIS,
        "uazapi_base_url": BASE_URL_FALSA,
        "uazapi_token": TOKEN_FALSO,
        # Explícito, e não herdado do default: estes testes falam do canal de
        # WhatsApp, e o teste que liga o e-mail o diz na própria chamada.
        "alertas_canais": Canal.WHATSAPP.value,
        "alertas_destinatarios": json.dumps({tipo.value: [DESTINATARIO_A] for tipo in TipoAlerta}),
    }
    base.update(overrides)
    return settings.model_copy(update=base)


@pytest.fixture
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture(autouse=True)
def limpar_alertas(settings: Settings) -> Iterator[None]:
    """Apaga as linhas de `alertas_enviados` deste teste, inclusive as do gancho.

    Duas condições, e a segunda entrou com o canal de e-mail: o destinatário
    deixou de ser sempre um dos dois telefones conhecidos, então o assunto
    (`chave`, marcada com `MARCA`) é o que acha as linhas do canal novo.
    """
    yield
    with get_sessionmaker()() as session:
        session.execute(
            text(
                "delete from alertas_enviados "
                "where destinatario = any(:numeros) or chave like :prefixo"
            ),
            {"numeros": list(NUMEROS_DO_TESTE), "prefixo": f"{PREFIXO_CHAVE}%"},
        )
        session.commit()


def _limpar_documentos(sessao: Session, ids: list[uuid.UUID]) -> None:
    """Apaga tudo o que pende de `ids`, na ordem que respeita as FKs."""
    for tabela in ("alertas_enviados", "pendencias", "validacoes", "extracoes", "log_conferencia"):
        sessao.execute(text(f"delete from {tabela} where documento_id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from documentos where id = any(:ids)"), {"ids": ids})


@dataclass
class Cenario:
    """Documentos e pendências que os detectores devem (ou não devem) enxergar."""

    operadora: Operadora
    paciente: Paciente
    critico: Documento
    """`incompleto` com pendência em campo crítico — o detector tem que pegar."""

    incompleto_nao_critico: Documento
    """`incompleto`, mas a pendência é de campo que não acorda ninguém."""

    problema_critico: Documento
    """Campo crítico, mas o documento está em `problema`, não `incompleto`."""

    pendencia_parada: Pendencia
    pendencia_recente: Pendencia
    pendencia_em_correcao: Pendencia
    documentos: list[uuid.UUID] = field(default_factory=list)


@pytest.fixture
def cenario(sessao: Session, settings: Settings) -> Iterator[Cenario]:
    agora = datetime.now(UTC)
    # Longe da janela de `deadline_competencia` para não contaminar aquele teste.
    deadline_distante = agora + timedelta(days=365)

    operadora = Operadora(nome="Operadora Alertas", codigo=f"ALERT-{uuid.uuid4()}")
    sessao.add(operadora)
    sessao.flush()
    paciente = Paciente(nome="Maria de Souza", operadora_id=operadora.id, modalidade=Modalidade.AD)
    sessao.add(paciente)
    sessao.flush()

    def _documento(status: DocumentoStatus, competencia: str = COMPETENCIA) -> Documento:
        documento = Documento(
            operadora_id=operadora.id,
            paciente_id=paciente.id,
            tipo=TipoDocumento.EVOLUCAO,
            arquivo_url=f"s3://fake/alertas/{uuid.uuid4()}",
            competencia=competencia,
            status=status,
        )
        sessao.add(documento)
        return documento

    critico = _documento(DocumentoStatus.INCOMPLETO)
    incompleto_nao_critico = _documento(DocumentoStatus.INCOMPLETO)
    problema_critico = _documento(DocumentoStatus.PROBLEMA)
    # Dois documentos com pendência dentro da janela de deadline, para a
    # contagem do alerta de competência ser verificável (2, não "alguns").
    deadline_1 = _documento(DocumentoStatus.INCOMPLETO, COMPETENCIA_DEADLINE)
    deadline_2 = _documento(DocumentoStatus.INCOMPLETO, COMPETENCIA_DEADLINE)
    fora_da_janela = _documento(DocumentoStatus.INCOMPLETO, COMPETENCIA_FORA_DA_JANELA)
    sessao.flush()

    def _pendencia(
        documento: Documento,
        *,
        campo: str,
        descricao: str,
        status: PendenciaStatus = PendenciaStatus.ABERTA,
        deadline: datetime | None = None,
        created_at: datetime | None = None,
    ) -> Pendencia:
        pendencia = Pendencia(
            documento_id=documento.id,
            tipo_problema="campo_ausente",
            campo=campo,
            descricao=descricao,
            responsavel="equipe-conferencia",
            status=status,
            deadline=deadline if deadline is not None else deadline_distante,
        )
        if created_at is not None:
            pendencia.created_at = created_at
        sessao.add(pendencia)
        return pendencia

    pendencia_parada = _pendencia(
        critico,
        campo="carimbo_presente",
        descricao="carimbo_presente: carimbo ausente",
        created_at=agora - timedelta(hours=72),
    )
    _pendencia(
        incompleto_nao_critico,
        campo="data_atendimento",
        descricao="data_atendimento: data ausente",
    )
    _pendencia(
        problema_critico,
        campo="assinatura_profissional_presente",
        descricao="assinatura_profissional_presente: assinatura ausente",
    )
    pendencia_recente = _pendencia(
        incompleto_nao_critico,
        campo="crm_profissional",
        descricao="crm_profissional: CRM ausente",
        created_at=agora - timedelta(hours=1),
    )
    pendencia_em_correcao = _pendencia(
        problema_critico,
        campo="carimbo_legivel",
        descricao="carimbo_legivel: carimbo ilegível",
        status=PendenciaStatus.EM_CORRECAO,
        created_at=agora - timedelta(hours=72),
    )
    # Campo NÃO crítico de propósito: estes três documentos existem para o
    # detector de deadline de competência, e um campo crítico os faria aparecer
    # também no detector de documento incompleto crítico.
    _pendencia(
        deadline_1,
        campo="data_atendimento",
        descricao="data_atendimento: data ausente",
        deadline=agora + timedelta(days=1),
    )
    _pendencia(
        deadline_2,
        campo="data_atendimento",
        descricao="data_atendimento: data ausente",
        deadline=agora + timedelta(days=1),
    )
    _pendencia(
        fora_da_janela,
        campo="data_atendimento",
        descricao="data_atendimento: data ausente",
        deadline=agora + timedelta(days=30),
    )
    sessao.commit()

    documentos = [
        critico.id,
        incompleto_nao_critico.id,
        problema_critico.id,
        deadline_1.id,
        deadline_2.id,
        fora_da_janela.id,
    ]
    yield Cenario(
        operadora=operadora,
        paciente=paciente,
        critico=critico,
        incompleto_nao_critico=incompleto_nao_critico,
        problema_critico=problema_critico,
        pendencia_parada=pendencia_parada,
        pendencia_recente=pendencia_recente,
        pendencia_em_correcao=pendencia_em_correcao,
        documentos=documentos,
    )

    _limpar_documentos(sessao, documentos)
    sessao.execute(text("delete from pacientes where id = :id"), {"id": paciente.id})
    sessao.execute(text("delete from operadoras where id = :id"), {"id": operadora.id})
    sessao.commit()


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """API com o gateway substituído pelo dublê — nenhuma requisição sai daqui."""
    app.dependency_overrides[get_settings] = lambda: _com_alertas(
        settings, alertas_destinatarios=""
    )
    app.dependency_overrides[obter_canais] = lambda: _canais(ProviderFake())
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _linhas(sessao: Session, destinatario: str) -> list[AlertaEnviado]:
    sessao.expire_all()
    return list(
        sessao.scalars(
            select(AlertaEnviado)
            .where(AlertaEnviado.destinatario == destinatario)
            .order_by(AlertaEnviado.created_at)
        ).all()
    )


def _alerta_de_teste(chave: str) -> Alerta:
    return Alerta(
        tipo=TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO,
        chave=chave,
        contexto={
            "paciente": "Maria de Souza",
            "operadora": "Operadora Alertas",
            "problema": "carimbo ausente",
            "deadline": "10/07/2097",
            "acao": "Reenviar a evolução com carimbo e assinatura.",
        },
    )


# --- autenticação -------------------------------------------------------------


def test_varredura_sem_x_api_key_responde_401(api: TestClient) -> None:
    assert api.post("/api/alertas/varredura").status_code == 401


def test_listar_alertas_sem_x_api_key_responde_401(api: TestClient) -> None:
    assert api.get("/api/alertas").status_code == 401


# --- detector: documento incompleto crítico ------------------------------------


def test_detector_critico_pega_incompleto_com_campo_critico_e_so_ele(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """Campo não crítico e documento em `problema` não acordam ninguém."""
    alertas = detectar_documento_incompleto_critico(sessao, settings)

    meus = [a for a in alertas if a.documento_id in cenario.documentos]

    assert [a.documento_id for a in meus] == [cenario.critico.id]
    (alerta,) = meus
    assert alerta.chave == f"documento:{cenario.critico.id}"
    assert alerta.contexto["paciente"] == "Maria de Souza"
    assert alerta.contexto["operadora"] == "Operadora Alertas"
    # A pendência do cenário é `campo="carimbo_presente"` — o rótulo humano
    # aparece, não o nome técnico da coluna nem a `descricao` da tela de
    # conferência (o nome do campo aparecia duplicado na mensagem).
    assert alerta.contexto["problema"] == "• Carimbo"


def test_detector_critico_filtrado_por_documento_ignora_os_demais(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """É o modo que o gancho da classificação usa: um documento, não a base inteira."""
    assert (
        detectar_documento_incompleto_critico(
            sessao, settings, documento_id=cenario.problema_critico.id
        )
        == []
    )
    alertas = detectar_documento_incompleto_critico(
        sessao, settings, documento_id=cenario.critico.id
    )
    assert [a.documento_id for a in alertas] == [cenario.critico.id]


def test_detector_critico_agrega_multiplas_pendencias_em_linhas_com_marcador(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """A mensagem real que motivou esta correção: três pendências críticas viravam
    uma parede de texto grudada por `" | "`. Agora cada uma é uma linha com
    marcador, na ordem em que foram abertas — e sem repetir o nome do campo."""
    agora = datetime.now(UTC)
    documento = Documento(
        operadora_id=cenario.operadora.id,
        paciente_id=cenario.paciente.id,
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url=f"s3://fake/alertas/{uuid.uuid4()}",
        competencia=COMPETENCIA,
        status=DocumentoStatus.INCOMPLETO,
    )
    sessao.add(documento)
    sessao.flush()
    for indice, campo in enumerate(
        ["assinatura_profissional_presente", "carimbo_presente", "carimbo_legivel"]
    ):
        sessao.add(
            Pendencia(
                documento_id=documento.id,
                tipo_problema="campo_invalido",
                campo=campo,
                descricao=f"{campo}: Campo '{campo}' não é verdadeiro.",
                responsavel="equipe-conferencia",
                status=PendenciaStatus.ABERTA,
                deadline=agora + timedelta(days=365),
                created_at=agora + timedelta(seconds=indice),
            )
        )
    sessao.commit()
    cenario.documentos.append(documento.id)

    alertas = detectar_documento_incompleto_critico(sessao, settings, documento_id=documento.id)

    (alerta,) = alertas
    assert alerta.contexto["problema"] == (
        "• Assinatura do profissional\n• Carimbo\n• Carimbo legível"
    )
    assert " | " not in alerta.contexto["problema"]
    # O nome técnico não aparece duplicado nem sozinho: só o rótulo.
    assert "carimbo_presente" not in alerta.contexto["problema"]


# --- envio e log --------------------------------------------------------------


def test_envio_grava_linha_enviada_com_a_mensagem_exata_que_o_gateway_recebeu(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    provider = ProviderFake()
    alertas = detectar_documento_incompleto_critico(
        sessao, settings, documento_id=cenario.critico.id
    )

    resumo = despachar(sessao, _com_alertas(settings), _canais(provider), alertas)

    assert resumo.enviados == 1
    assert resumo.provider_configurado is True
    (destinatario, mensagem) = provider.enviadas[0]
    assert destinatario == DESTINATARIO_A
    assert "Maria de Souza" in mensagem
    (linha,) = _linhas(sessao, DESTINATARIO_A)
    assert linha.status == StatusAlerta.ENVIADO.value
    assert linha.mensagem == mensagem
    assert linha.documento_id == cenario.critico.id
    assert linha.detalhe is None


def test_cooldown_suprime_em_silencio_sem_gravar_linha_nova(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """Duas varreduras seguidas avisam uma vez — e a segunda não polui a tabela."""
    provider = ProviderFake()
    alertas = [_alerta_de_teste(_chave("cooldown"))]
    resolvido = _com_alertas(settings)

    primeiro = despachar(sessao, resolvido, _canais(provider), alertas)
    segundo = despachar(sessao, resolvido, _canais(provider), alertas)

    assert primeiro.enviados == 1
    assert segundo.enviados == 0
    assert segundo.suprimidos == 1
    assert len(provider.enviadas) == 1
    linhas = _linhas(sessao, DESTINATARIO_A)
    assert len(linhas) == 1
    assert linhas[0].status == StatusAlerta.ENVIADO.value


def test_rate_limit_grava_linha_suprimida_com_o_motivo(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """Esta supressão é anômala: alguém precisa poder descobrir que perdeu alerta."""
    provider = ProviderFake()
    resolvido = _com_alertas(settings, alertas_max_por_hora_por_destinatario=1)
    alertas = [_alerta_de_teste(_chave("primeiro")), _alerta_de_teste(_chave("segundo"))]

    resumo = despachar(sessao, resolvido, _canais(provider), alertas)

    assert resumo.enviados == 1
    assert resumo.suprimidos == 1
    assert len(provider.enviadas) == 1
    enviada, suprimida = _linhas(sessao, DESTINATARIO_A)
    assert enviada.status == StatusAlerta.ENVIADO.value
    assert suprimida.status == StatusAlerta.SUPRIMIDO.value
    assert suprimida.detalhe is not None
    assert "rate limit" in suprimida.detalhe


def test_falha_de_envio_registra_e_nao_interrompe_o_proximo_destinatario(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    provider = ProviderFake(falhar_para={DESTINATARIO_A})
    resolvido = _com_alertas(
        settings,
        alertas_destinatarios=json.dumps(
            {TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO.value: [DESTINATARIO_A, DESTINATARIO_B]}
        ),
    )

    resumo = despachar(sessao, resolvido, _canais(provider), [_alerta_de_teste(_chave("falha"))])

    assert resumo.falhas == 1
    assert resumo.enviados == 1
    assert [destinatario for destinatario, _ in provider.enviadas] == [DESTINATARIO_B]
    (falhou,) = _linhas(sessao, DESTINATARIO_A)
    assert falhou.status == StatusAlerta.FALHA.value
    assert falhou.detalhe is not None
    assert "401" in falhou.detalhe
    (enviou,) = _linhas(sessao, DESTINATARIO_B)
    assert enviou.status == StatusAlerta.ENVIADO.value


def test_sem_provider_configurado_nada_e_enviado_nem_gravado(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    resumo = despachar(
        sessao,
        _com_alertas(settings),
        _canais(provider=None),
        [_alerta_de_teste(_chave("sem-credencial"))],
    )

    assert resumo.provider_configurado is False
    assert resumo.enviados == 0
    assert resumo.detectados == 1
    assert _linhas(sessao, DESTINATARIO_A) == []


# --- demais detectores --------------------------------------------------------


def test_deadline_competencia_conta_documentos_da_janela_e_ignora_os_de_fora(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    alertas = detectar_deadline_competencia(sessao, settings)
    por_chave = {alerta.chave: alerta for alerta in alertas}

    dentro = por_chave[f"competencia:{COMPETENCIA_DEADLINE}:{cenario.operadora.id}"]
    assert dentro.contexto["documentos"] == "2"
    assert dentro.contexto["competencia"] == COMPETENCIA_DEADLINE
    assert dentro.contexto["operadora"] == "Operadora Alertas"
    assert f"competencia:{COMPETENCIA_FORA_DA_JANELA}:{cenario.operadora.id}" not in por_chave


def test_pendencia_parada_pega_aberta_antiga_e_ignora_recente_e_em_correcao(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """`em_correcao` não é pendência parada: quem já está corrigindo não está parado."""
    alertas = detectar_pendencia_parada(sessao, settings)
    chaves = {alerta.chave for alerta in alertas}

    assert f"pendencia:{cenario.pendencia_parada.id}" in chaves
    assert f"pendencia:{cenario.pendencia_recente.id}" not in chaves
    assert f"pendencia:{cenario.pendencia_em_correcao.id}" not in chaves
    parada = next(a for a in alertas if a.chave == f"pendencia:{cenario.pendencia_parada.id}")
    assert int(parada.contexto["horas"]) >= 72
    assert parada.contexto["problema"] == "Carimbo"


def test_pendencia_parada_com_campo_fora_do_vocabulario_ainda_produz_alerta(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """Campo que o vocabulário não conhece (regra nova, criada pela
    API depois desta entrega) não pode fazer o alerta sumir — cai no fallback
    previsível do próprio nome técnico."""
    agora = datetime.now(UTC)
    documento = Documento(
        operadora_id=cenario.operadora.id,
        paciente_id=cenario.paciente.id,
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url=f"s3://fake/alertas/{uuid.uuid4()}",
        competencia=COMPETENCIA,
        status=DocumentoStatus.INCOMPLETO,
    )
    sessao.add(documento)
    sessao.flush()
    pendencia = Pendencia(
        documento_id=documento.id,
        tipo_problema="campo_invalido",
        campo="campo_novo_sem_rotulo",
        descricao="campo_novo_sem_rotulo: alguma coisa não está certa",
        responsavel="equipe-conferencia",
        status=PendenciaStatus.ABERTA,
        deadline=agora + timedelta(days=365),
        created_at=agora - timedelta(hours=72),
    )
    sessao.add(pendencia)
    sessao.commit()
    cenario.documentos.append(documento.id)

    alertas = detectar_pendencia_parada(sessao, settings)
    encontrado = next(a for a in alertas if a.chave == f"pendencia:{pendencia.id}")

    assert encontrado.contexto["problema"] == "campo_novo_sem_rotulo"


def test_volume_anormal_nao_dispara_abaixo_do_piso_mesmo_com_taxa_de_100_por_cento(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """A regressão que protege a equipe de alerta diário inútil.

    O piso é elevado (em vez de o volume ser reduzido) porque o banco é
    compartilhado: quantos documentos existem "hoje" não está sob controle
    deste teste, e um piso acima de qualquer volume plausível é a única forma
    determinística de exercitar a guarda.
    """
    resolvido = settings.model_copy(
        update={"alertas_volume_minimo_documentos": 10_000_000, "alertas_volume_fator": 0.0}
    )

    assert detectar_volume_anormal(sessao, resolvido) == []


# --- endpoints ----------------------------------------------------------------


def test_varredura_pelo_endpoint_devolve_o_resumo_com_os_quatro_tipos(
    api: TestClient, cenario: Cenario
) -> None:
    """Sem destinatário configurado nada é enviado: o resumo é o contrato exercitado."""
    resposta = api.post("/api/alertas/varredura", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["provider_configurado"] is True
    assert corpo["enviados"] == 0
    assert set(corpo["por_tipo"]) == {tipo.value for tipo in TipoAlerta}
    assert corpo["por_tipo"][TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO.value] >= 1


def test_varredura_com_destinatario_invalido_responde_422_dizendo_o_que_consertar(
    settings: Settings, cenario: Cenario
) -> None:
    app.dependency_overrides[get_settings] = lambda: _com_alertas(
        settings, alertas_destinatarios=json.dumps({"deadline_competencias": ["5521999999999"]})
    )
    app.dependency_overrides[obter_canais] = lambda: _canais(ProviderFake())
    try:
        resposta = TestClient(app).post("/api/alertas/varredura", headers=AUTH_HEADERS)
    finally:
        app.dependency_overrides.clear()

    assert resposta.status_code == 422
    assert "deadline_competencias" in resposta.json()["error"]["mensagem"]


def test_listar_alertas_pagina_filtra_e_ordena_do_mais_recente_para_o_mais_antigo(
    api: TestClient, sessao: Session, cenario: Cenario
) -> None:
    agora = datetime.now(UTC)
    for indice, (tipo, status_alerta) in enumerate(
        [
            (TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO, StatusAlerta.ENVIADO),
            (TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO, StatusAlerta.FALHA),
            (TipoAlerta.PENDENCIA_PARADA, StatusAlerta.ENVIADO),
        ]
    ):
        sessao.add(
            AlertaEnviado(
                tipo=tipo.value,
                canal=Canal.WHATSAPP.value,
                chave=f"documento:{cenario.critico.id}",
                destinatario=DESTINATARIO_A,
                mensagem=f"mensagem {indice}",
                status=status_alerta.value,
                documento_id=cenario.critico.id,
                created_at=agora - timedelta(minutes=indice),
            )
        )
    sessao.commit()

    todos = api.get(f"/api/alertas?documento_id={cenario.critico.id}", headers=AUTH_HEADERS).json()
    por_tipo = api.get(
        f"/api/alertas?documento_id={cenario.critico.id}&tipo={TipoAlerta.PENDENCIA_PARADA.value}",
        headers=AUTH_HEADERS,
    ).json()
    por_status = api.get(
        f"/api/alertas?documento_id={cenario.critico.id}&status={StatusAlerta.FALHA.value}",
        headers=AUTH_HEADERS,
    ).json()

    assert todos["paginacao"]["total"] == 3
    assert [item["mensagem"] for item in todos["data"]] == [
        "mensagem 0",
        "mensagem 1",
        "mensagem 2",
    ]
    assert [item["tipo"] for item in por_tipo["data"]] == [TipoAlerta.PENDENCIA_PARADA.value]
    assert [item["status"] for item in por_status["data"]] == [StatusAlerta.FALHA.value]


# --- gancho na classificação --------------------------------------------------


def test_gancho_que_explode_nao_impede_a_classificacao_de_commitar(
    sessao: Session,
    settings: Settings,
    cenario: Cenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garantia inegociável: notificação nunca derruba ingestão de documento.

    O dublê levanta `RuntimeError` — erro que o serviço de alertas **não**
    trata — para exercitar a blindagem externa do gancho, e não o caminho feliz
    de `EnvioError`.
    """
    monkeypatch.setattr(hooks, "get_settings", lambda: _com_alertas(settings))
    monkeypatch.setattr(
        hooks, "construir_canais", lambda _session, _settings: _canais(ProviderQueExplode())
    )
    documento = Documento(
        operadora_id=cenario.operadora.id,
        paciente_id=cenario.paciente.id,
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url=f"s3://fake/alertas/{uuid.uuid4()}",
        competencia=COMPETENCIA,
        status=DocumentoStatus.PROCESSANDO,
    )
    sessao.add(documento)
    sessao.commit()
    cenario.documentos.append(documento.id)

    status_final = classificar_documento(
        sessao,
        documento.id,
        [
            ResultadoAvaliacao(
                campo="carimbo_presente",
                regra_id=uuid.uuid4(),
                resultado=ResultadoValidacao.REPROVADO,
                detalhe="carimbo ausente",
                acao=AcaoRegra.REJEITAR,
                motivo_glosa="Evolução sem carimbo",
            )
        ],
        usuario="teste",
    )

    assert status_final is DocumentoStatus.INCOMPLETO
    sessao.expire_all()
    persistido = sessao.get(Documento, documento.id)
    assert persistido is not None
    assert persistido.status is DocumentoStatus.INCOMPLETO
    pendencia = sessao.scalars(
        select(Pendencia).where(Pendencia.documento_id == documento.id)
    ).one()
    assert pendencia.campo == "carimbo_presente"
    assert pendencia.status is PendenciaStatus.ABERTA


def test_gancho_desligado_nao_envia_nada(
    sessao: Session,
    settings: Settings,
    cenario: Cenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ALERTAS_HOOK_INLINE_HABILITADO=false` deixa o caso para a varredura."""
    provider = ProviderFake()
    monkeypatch.setattr(
        hooks,
        "get_settings",
        lambda: _com_alertas(settings, alertas_hook_inline_habilitado=False),
    )
    monkeypatch.setattr(hooks, "construir_canais", lambda _session, _settings: _canais(provider))

    hooks.notificar_classificacao(cenario.critico.id)

    assert provider.enviadas == []
    assert _linhas(sessao, DESTINATARIO_A) == []


def test_gancho_habilitado_notifica_o_documento_critico(
    sessao: Session,
    settings: Settings,
    cenario: Cenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ProviderFake()
    monkeypatch.setattr(hooks, "get_settings", lambda: _com_alertas(settings))
    monkeypatch.setattr(hooks, "construir_canais", lambda _session, _settings: _canais(provider))

    hooks.notificar_classificacao(cenario.critico.id)

    assert [destinatario for destinatario, _ in provider.enviadas] == [DESTINATARIO_A]
    (linha,) = _linhas(sessao, DESTINATARIO_A)
    assert linha.status == StatusAlerta.ENVIADO.value
    assert linha.documento_id == cenario.critico.id


# --- ADR 0006: dois canais ------------------------------------------------------


@pytest.fixture
def pessoas(sessao: Session) -> Iterator[dict[str, Usuario]]:
    """Um coordenador ativo, um coordenador desativado e um gestor, só deste teste.

    O teardown apaga as linhas de `alertas_enviados` que apontam para eles
    **antes** dos próprios usuários: `alertas_enviados.usuario_id` é FK, e a
    ordem inversa deixaria o `delete from usuarios` bater na constraint.
    """
    criados = {
        "coordenador": Usuario(
            nome="Coordenação Teste",
            email=f"coord-{uuid.uuid4()}@teste.local",
            senha_hash=senhas.gerar_hash("senha-de-teste-longa"),
            papel=Papel.COORDENADOR.value,
            ativo=True,
        ),
        "coordenador_desativado": Usuario(
            nome="Coordenação Que Saiu",
            email=f"saiu-{uuid.uuid4()}@teste.local",
            senha_hash=senhas.gerar_hash("senha-de-teste-longa"),
            papel=Papel.COORDENADOR.value,
            ativo=False,
        ),
        "gestor": Usuario(
            nome="Gestão Teste",
            email=f"gestao-{uuid.uuid4()}@teste.local",
            senha_hash=senhas.gerar_hash("senha-de-teste-longa"),
            papel=Papel.GESTOR.value,
            ativo=True,
        ),
    }
    for usuario in criados.values():
        sessao.add(usuario)
    sessao.commit()
    ids = [usuario.id for usuario in criados.values()]

    yield criados

    with get_sessionmaker()() as limpeza:
        limpeza.execute(
            text("delete from alertas_enviados where usuario_id = any(:ids)"), {"ids": ids}
        )
        limpeza.execute(text("delete from usuarios where id = any(:ids)"), {"ids": ids})
        limpeza.commit()


def _destinatario(usuario: Usuario) -> Destinatario:
    return Destinatario(endereco=usuario.email, usuario_id=usuario.id)


def _canal_email(
    destinatarios: list[Destinatario],
) -> tuple[ProviderEmailFake, CanalEmailComDestinatarioFixo]:
    provider = ProviderEmailFake()
    return provider, CanalEmailComDestinatarioFixo(provider=provider, destinatarios=destinatarios)


def _linhas_de(sessao: Session, chave: str) -> list[AlertaEnviado]:
    sessao.expire_all()
    return list(
        sessao.scalars(
            select(AlertaEnviado).where(AlertaEnviado.chave == chave).order_by(AlertaEnviado.canal)
        ).all()
    )


def test_alerta_sai_pelos_dois_canais_com_uma_linha_por_canal(
    sessao: Session, settings: Settings, cenario: Cenario, pessoas: dict[str, Usuario]
) -> None:
    """Critério 1: dois canais ligados e com credencial = dois envios, duas linhas
    distinguíveis. Não é fallback — é o comportamento desejado (ADR 0006)."""
    coordenador = pessoas["coordenador"]
    whatsapp = ProviderFake()
    email, canal_email = _canal_email([_destinatario(coordenador)])
    chave = _chave("dois-canais")

    resumo = despachar(
        sessao,
        _com_alertas(settings, alertas_canais="whatsapp,email"),
        _canais(whatsapp, email=canal_email),
        [_alerta_de_teste(chave)],
    )

    assert resumo.enviados == 2
    assert [destinatario for destinatario, _ in whatsapp.enviadas] == [DESTINATARIO_A]
    assert [destinatario for destinatario, _, _ in email.enviadas] == [coordenador.email]

    por_canal = {linha.canal: linha for linha in _linhas_de(sessao, chave)}
    assert set(por_canal) == {Canal.WHATSAPP.value, Canal.EMAIL.value}
    assert por_canal[Canal.WHATSAPP.value].destinatario == DESTINATARIO_A
    assert por_canal[Canal.WHATSAPP.value].usuario_id is None
    assert por_canal[Canal.EMAIL.value].destinatario == coordenador.email
    assert por_canal[Canal.EMAIL.value].usuario_id == coordenador.id
    for linha in por_canal.values():
        assert linha.status == StatusAlerta.ENVIADO.value


def test_o_email_chega_sem_asterisco_literal_e_com_assunto_proprio(
    sessao: Session, settings: Settings, cenario: Cenario, pessoas: dict[str, Usuario]
) -> None:
    """Critério 4, do lado do despacho: o texto que o gateway de e-mail recebe é
    texto puro e tem assunto; o do WhatsApp continua com emoji e `*negrito*`."""
    coordenador = pessoas["coordenador"]
    whatsapp = ProviderFake()
    email, canal_email = _canal_email([_destinatario(coordenador)])

    despachar(
        sessao,
        _com_alertas(settings, alertas_canais="whatsapp,email"),
        _canais(whatsapp, email=canal_email),
        [_alerta_de_teste(_chave("texto"))],
    )

    (_, texto_whatsapp) = whatsapp.enviadas[0]
    (_, assunto, corpo) = email.enviadas[0]
    assert texto_whatsapp.startswith("🚨 *Pendência crítica*")
    assert assunto == "Pendência crítica — Operadora Alertas"
    assert "*" not in assunto
    assert "*" not in corpo
    assert "🚨" not in corpo
    # O corpo é o mesmo conteúdo, sem a marcação: os dados continuam lá.
    assert "Operadora: Operadora Alertas" in corpo
    assert "carimbo ausente" in corpo


def test_o_log_do_email_guarda_o_assunto_junto_com_o_corpo(
    sessao: Session, settings: Settings, cenario: Cenario, pessoas: dict[str, Usuario]
) -> None:
    """Auditar um envio é saber o que foi dito, e no e-mail o assunto é parte
    disso — omiti-lo deixaria o log respondendo pela metade."""
    coordenador = pessoas["coordenador"]
    email, canal_email = _canal_email([_destinatario(coordenador)])
    chave = _chave("log-com-assunto")

    despachar(
        sessao,
        _com_alertas(settings, alertas_canais="email"),
        _canais(provider=None, email=canal_email, whatsapp_habilitado=False),
        [_alerta_de_teste(chave)],
    )

    (linha,) = _linhas_de(sessao, chave)
    (_, assunto, corpo) = email.enviadas[0]
    assert linha.mensagem == f"Assunto: {assunto}\n\n{corpo}"


def test_canal_habilitado_sem_credencial_nao_envia_nao_estoura_e_aparece_no_resumo(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """Critério 2. Ligar o e-mail sem SMTP é o mesmo modo de falha que a
    recuperação de senha já tem — e lá a única pista é uma linha de log."""
    chave = _chave("sem-credencial-email")

    resumo = despachar(
        sessao,
        _com_alertas(settings, alertas_canais="whatsapp,email"),
        [
            CanalWhatsApp(habilitado=True, provider=ProviderFake()),
            CanalEmail(habilitado=True, provider=None),
        ],
        [_alerta_de_teste(chave)],
    )

    assert resumo.enviados == 1
    assert resumo.falhas == 0
    assert resumo.canais[Canal.EMAIL.value].habilitado is True
    assert resumo.canais[Canal.EMAIL.value].disponivel is False
    assert [linha.canal for linha in _linhas_de(sessao, chave)] == [Canal.WHATSAPP.value]


def test_canal_desabilitado_nao_envia_nem_grava_linha(
    sessao: Session, settings: Settings, cenario: Cenario, pessoas: dict[str, Usuario]
) -> None:
    """Critério 3. Desligado é diferente de sem credencial, e o resumo diz qual."""
    email, canal_email = _canal_email([_destinatario(pessoas["coordenador"])])
    canal_email.habilitado = False
    chave = _chave("email-desligado")

    resumo = despachar(
        sessao,
        _com_alertas(settings, alertas_canais="whatsapp"),
        _canais(ProviderFake(), email=canal_email),
        [_alerta_de_teste(chave)],
    )

    assert email.enviadas == []
    assert resumo.canais[Canal.EMAIL.value].habilitado is False
    assert resumo.canais[Canal.EMAIL.value].disponivel is True
    assert [linha.canal for linha in _linhas_de(sessao, chave)] == [Canal.WHATSAPP.value]


def test_nenhum_canal_ligado_nao_envia_nada_e_o_resumo_ainda_conta_o_detectado(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    chave = _chave("tudo-desligado")

    resumo = despachar(
        sessao,
        _com_alertas(settings, alertas_canais=""),
        [
            CanalWhatsApp(habilitado=False, provider=ProviderFake()),
            CanalEmail(habilitado=False, provider=ProviderEmailFake()),
        ],
        [_alerta_de_teste(chave)],
    )

    assert resumo.detectados == 1
    assert resumo.enviados == 0
    assert resumo.provider_configurado is False
    assert _linhas_de(sessao, chave) == []


def test_falha_de_um_canal_nao_impede_o_outro(
    sessao: Session, settings: Settings, cenario: Cenario, pessoas: dict[str, Usuario]
) -> None:
    """Um gateway fora do ar não pode silenciar o canal que está inteiro — é o
    argumento que justifica haver dois."""
    coordenador = pessoas["coordenador"]
    whatsapp = ProviderFake(falhar_para={DESTINATARIO_A})
    _, canal_email = _canal_email([_destinatario(coordenador)])
    chave = _chave("falha-parcial")

    resumo = despachar(
        sessao,
        _com_alertas(settings, alertas_canais="whatsapp,email"),
        _canais(whatsapp, email=canal_email),
        [_alerta_de_teste(chave)],
    )

    assert resumo.falhas == 1
    assert resumo.enviados == 1
    por_canal = {linha.canal: linha for linha in _linhas_de(sessao, chave)}
    assert por_canal[Canal.WHATSAPP.value].status == StatusAlerta.FALHA.value
    assert por_canal[Canal.EMAIL.value].status == StatusAlerta.ENVIADO.value


# --- o anti-bombardeio conta a pessoa, não o endereço ---------------------------


def test_o_teto_por_hora_nao_dobra_ao_ligar_o_segundo_canal(
    sessao: Session, settings: Settings, cenario: Cenario, pessoas: dict[str, Usuario]
) -> None:
    """**Critério 6, o que justifica a migration.**

    Duas linhas em endereços diferentes da MESMA pessoa somam num teto só. Se o
    rate limit continuasse contando por endereço, ligar o segundo canal daria a
    quem recebe nos dois o dobro de mensagens por hora — sem ninguém pedir, e
    sem erro nenhum aparecer.

    A linha semeada é de WhatsApp e **atribuída à pessoa**. Hoje o telefone do
    `.env` não tem dono (não há telefone em `usuarios`, ADR 0006 §3), então essa
    forma é a que a segunda parte do ADR e o dia em que o cadastro tiver
    telefone produzem. O que este teste fixa é a REGRA DE CONTAGEM — que é
    exatamente o que a coluna `usuario_id` existe para permitir.

    O contraste está na mesma passada: a linha de `sem_dono` tem
    `usuario_id NULL`, como toda linha de telefone avulso, e por isso **não**
    entra no teto de ninguém.
    """
    com_dono = pessoas["coordenador"]
    sem_dono = pessoas["gestor"]
    agora = datetime.now(UTC)
    for usuario_id in (com_dono.id, None):
        sessao.add(
            AlertaEnviado(
                tipo=TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO.value,
                canal=Canal.WHATSAPP.value,
                chave=_chave("aviso-anterior"),
                destinatario=DESTINATARIO_B,
                usuario_id=usuario_id,
                mensagem="aviso que já saiu nesta hora",
                status=StatusAlerta.ENVIADO.value,
                created_at=agora - timedelta(minutes=5),
            )
        )
    sessao.commit()

    email, canal_email = _canal_email([_destinatario(com_dono), _destinatario(sem_dono)])
    chave = _chave("teto")

    resumo = despachar(
        sessao,
        _com_alertas(settings, alertas_canais="email", alertas_max_por_hora_por_destinatario=1),
        _canais(provider=None, email=canal_email, whatsapp_habilitado=False),
        [_alerta_de_teste(chave)],
    )

    assert resumo.enviados == 1
    assert resumo.suprimidos == 1
    # Quem já tinha uma linha atribuída a si nesta hora não recebe a segunda.
    assert [destinatario for destinatario, _, _ in email.enviadas] == [sem_dono.email]
    por_endereco = {linha.destinatario: linha for linha in _linhas_de(sessao, chave)}
    assert por_endereco[com_dono.email].status == StatusAlerta.SUPRIMIDO.value
    assert por_endereco[com_dono.email].detalhe is not None
    assert "rate limit" in por_endereco[com_dono.email].detalhe
    assert por_endereco[sem_dono.email].status == StatusAlerta.ENVIADO.value


def test_o_teto_segue_a_pessoa_quando_o_endereco_dela_muda(
    sessao: Session, settings: Settings, cenario: Cenario, pessoas: dict[str, Usuario]
) -> None:
    """A mesma regra, sem nenhuma linha semeada: o e-mail de alguém é editável
    (`PATCH /api/usuarios/{id}`), e duas linhas em endereços diferentes dentro
    da hora continuam sendo a mesma pessoa recebendo duas mensagens."""
    coordenador = pessoas["coordenador"]
    endereco_antigo = coordenador.email
    endereco_novo = f"novo-{uuid.uuid4()}@teste.local"
    resolvido = _com_alertas(
        settings, alertas_canais="email", alertas_max_por_hora_por_destinatario=1
    )

    _, canal_antigo = _canal_email(
        [Destinatario(endereco=endereco_antigo, usuario_id=coordenador.id)]
    )
    primeiro = despachar(
        sessao,
        resolvido,
        _canais(provider=None, email=canal_antigo, whatsapp_habilitado=False),
        [_alerta_de_teste(_chave("antes-da-troca"))],
    )

    email_novo, canal_novo = _canal_email(
        [Destinatario(endereco=endereco_novo, usuario_id=coordenador.id)]
    )
    segundo = despachar(
        sessao,
        resolvido,
        _canais(provider=None, email=canal_novo, whatsapp_habilitado=False),
        [_alerta_de_teste(_chave("depois-da-troca"))],
    )

    assert primeiro.enviados == 1
    assert segundo.enviados == 0
    assert segundo.suprimidos == 1
    assert email_novo.enviadas == []


def test_o_cooldown_deixa_o_mesmo_aviso_sair_nos_dois_canais_e_uma_vez_em_cada(
    sessao: Session, settings: Settings, cenario: Cenario, pessoas: dict[str, Usuario]
) -> None:
    """Critério 7. O cooldown continua por **destinatário**: dois canais são dois
    endereços, e o mesmo aviso sair nos dois é o desejado — o que ele impede é a
    segunda varredura repetir o aviso no mesmo canal."""
    coordenador = pessoas["coordenador"]
    whatsapp = ProviderFake()
    email, canal_email = _canal_email([_destinatario(coordenador)])
    resolvido = _com_alertas(settings, alertas_canais="whatsapp,email")
    chave = _chave("cooldown-dois-canais")
    alertas = [_alerta_de_teste(chave)]

    primeiro = despachar(sessao, resolvido, _canais(whatsapp, email=canal_email), alertas)
    segundo = despachar(sessao, resolvido, _canais(whatsapp, email=canal_email), alertas)

    assert primeiro.enviados == 2
    assert segundo.enviados == 0
    assert segundo.suprimidos == 2
    assert len(whatsapp.enviadas) == 1
    assert len(email.enviadas) == 1
    # A supressão por cooldown não grava linha: continuam as duas do primeiro.
    assert len(_linhas_de(sessao, chave)) == 2


# --- destinatário de e-mail por papel ------------------------------------------


def test_email_por_papel_traz_a_conta_ativa_e_nunca_a_desativada(
    sessao: Session, pessoas: dict[str, Usuario]
) -> None:
    """Critério 5. Desativar é o caminho de saída de alguém da operação; continuar
    mandando pendência de paciente para quem saiu é vazamento, não só ruído.

    Consulta só de leitura: nada é enviado e nenhuma linha é gravada — ver a
    docstring do módulo para por que o despacho por papel não roda aqui.
    """
    encontrados = repository.usuarios_ativos_por_papel(sessao, papeis=[Papel.COORDENADOR])

    enderecos = {destinatario.endereco for destinatario in encontrados}
    assert pessoas["coordenador"].email in enderecos
    assert pessoas["coordenador_desativado"].email not in enderecos
    assert pessoas["gestor"].email not in enderecos
    (meu,) = [d for d in encontrados if d.endereco == pessoas["coordenador"].email]
    assert meu.usuario_id == pessoas["coordenador"].id


def test_email_por_papel_sem_papel_nenhum_devolve_vazio(sessao: Session) -> None:
    """É a mesma lista vazia que um papel sem nenhuma conta ativa produz — e o
    banco é compartilhado, então "papel sem ninguém" não é forçável aqui."""
    assert repository.usuarios_ativos_por_papel(sessao, papeis=[]) == []


def test_o_canal_de_email_real_resolve_o_papel_configurado_para_cada_tipo(
    sessao: Session, settings: Settings, pessoas: dict[str, Usuario]
) -> None:
    """O `CanalEmail` de produção, sem destinatário fixado — só leitura.

    Confere o default declarado: item individual vai ao coordenador, e o gestor
    entra apenas em `volume_anormal`, o único sinal agregado dos quatro.
    """
    canal = CanalEmail(habilitado=True, provider=ProviderEmailFake())
    resolvido = _com_alertas(settings, alertas_canais="email")

    individual = {
        d.endereco
        for d in canal.destinatarios(sessao, resolvido, TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO)
    }
    agregado = {
        d.endereco for d in canal.destinatarios(sessao, resolvido, TipoAlerta.VOLUME_ANORMAL)
    }

    assert pessoas["coordenador"].email in individual
    assert pessoas["gestor"].email not in individual
    assert pessoas["coordenador"].email in agregado
    assert pessoas["gestor"].email in agregado
    assert pessoas["coordenador_desativado"].email not in agregado


def test_tipo_sem_destinatario_no_canal_de_email_nao_derruba_a_varredura(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    """Critério 5, última parte. Papel sem nenhuma conta ativa produz a mesma
    lista vazia que uma configuração explícita de lista vazia: o tipo
    simplesmente não sai por esse canal, e a varredura segue para os outros."""
    whatsapp = ProviderFake()
    canal_email = CanalEmail(habilitado=True, provider=ProviderEmailFake())
    chave = _chave("papel-vazio")

    resumo = despachar(
        sessao,
        _com_alertas(
            settings,
            alertas_canais="whatsapp,email",
            alertas_papeis_email=json.dumps({TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO.value: []}),
        ),
        _canais(whatsapp, email=canal_email),
        [_alerta_de_teste(chave)],
    )

    assert resumo.enviados == 1
    assert resumo.falhas == 0
    assert [linha.canal for linha in _linhas_de(sessao, chave)] == [Canal.WHATSAPP.value]


# --- resumo da varredura --------------------------------------------------------


def test_o_resumo_do_endpoint_traz_os_dois_estados_de_cada_canal(
    api: TestClient, cenario: Cenario
) -> None:
    resposta = api.post("/api/alertas/varredura", headers=AUTH_HEADERS)

    canais = resposta.json()["canais"]
    assert set(canais) == {canal.value for canal in Canal}
    assert canais[Canal.WHATSAPP.value] == {"habilitado": True, "disponivel": True}
    assert canais[Canal.EMAIL.value] == {"habilitado": False, "disponivel": False}


def test_o_log_do_endpoint_expoe_o_canal_de_cada_linha(
    api: TestClient, sessao: Session, cenario: Cenario
) -> None:
    """Sem `canal` na resposta, duas linhas do mesmo aviso para a mesma pessoa
    seriam indistinguíveis — e é isso que o segundo canal produz de propósito."""
    sessao.add(
        AlertaEnviado(
            tipo=TipoAlerta.PENDENCIA_PARADA.value,
            canal=Canal.EMAIL.value,
            chave=_chave("log-canal"),
            destinatario="alguem@teste.local",
            mensagem="Assunto: x\n\ny",
            status=StatusAlerta.ENVIADO.value,
            documento_id=cenario.critico.id,
        )
    )
    sessao.commit()

    corpo = api.get(f"/api/alertas?documento_id={cenario.critico.id}", headers=AUTH_HEADERS).json()

    assert [item["canal"] for item in corpo["data"]] == [Canal.EMAIL.value]


def test_mensagem_renderizada_do_email_vira_o_registro_com_assunto() -> None:
    """Contrato de `MensagemAlerta.para_registro`, exercitado sem banco."""
    assert MensagemAlerta(corpo="só texto").para_registro() == "só texto"
    assert (
        MensagemAlerta(assunto="Prazo", corpo="corpo").para_registro() == "Assunto: Prazo\n\ncorpo"
    )


# --- configuração quebrada tem de chegar a quem opera --------------------------


def test_canal_invalido_em_alertas_canais_nao_derruba_mais_a_varredura(
    settings: Settings, cenario: Cenario
) -> None:
    """Depois da parte 2 do ADR 0006, `ALERTAS_CANAIS` **não decide nada**.

    Antes desta entrega um typo ali virava 422 e a varredura não rodava, o que
    era o certo: a variável desligava canal. Agora o liga/desliga vem da tabela
    `canais_alerta`, e recusar a varredura por causa de uma variável inerte
    trocaria uma configuração morta com erro de digitação por uma operação sem
    aviso — que é o desfecho que a trilha inteira existe para evitar. O typo
    ainda importa em um lugar só, e lá ele para o deploy: a migration que
    semeou a tabela (`a4d6c8b21f37`).
    """
    app.dependency_overrides[get_settings] = lambda: _com_alertas(
        settings, alertas_canais="whatsapp,telegrama"
    )
    try:
        resposta = TestClient(app).post("/api/alertas/varredura", headers=AUTH_HEADERS)
    finally:
        app.dependency_overrides.clear()

    assert resposta.status_code == 200
    assert set(resposta.json()["canais"]) == {canal.value for canal in Canal}


def test_papel_invalido_responde_422_dizendo_o_que_consertar(
    settings: Settings, cenario: Cenario
) -> None:
    app.dependency_overrides[get_settings] = lambda: _com_alertas(
        settings,
        alertas_papeis_email=json.dumps({TipoAlerta.VOLUME_ANORMAL.value: ["diretor"]}),
    )
    app.dependency_overrides[obter_canais] = lambda: _canais(ProviderFake())
    try:
        resposta = TestClient(app).post("/api/alertas/varredura", headers=AUTH_HEADERS)
    finally:
        app.dependency_overrides.clear()

    assert resposta.status_code == 422
    assert "diretor" in resposta.json()["error"]["mensagem"]


def test_o_cron_sai_com_codigo_1_e_mensagem_quando_a_configuracao_esta_quebrada(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Código 1 é a única situação em que alguém precisa acordar: "a configuração
    está quebrada e ninguém está sendo avisado". Um traceback no lugar dele daria
    o mesmo código de saída sem a mensagem que diz o que consertar.

    O typo vai em `ALERTAS_PAPEIS_EMAIL`, que continua decidindo quem recebe.
    `ALERTAS_CANAIS` saiu desta lista com a parte 2 do ADR 0006 — ver
    `test_canal_invalido_em_alertas_canais_nao_derruba_mais_a_varredura`.
    """
    monkeypatch.setattr(
        scan,
        "get_settings",
        lambda: _com_alertas(
            settings,
            alertas_papeis_email=json.dumps({TipoAlerta.VOLUME_ANORMAL.value: ["diretor"]}),
        ),
    )

    codigo = scan.main()

    assert codigo == 1
    assert "diretor" in capsys.readouterr().err
