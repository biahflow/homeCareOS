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

from homecareos.alerts import hooks
from homecareos.alerts.detectores import (
    detectar_deadline_competencia,
    detectar_documento_incompleto_critico,
    detectar_pendencia_parada,
    detectar_volume_anormal,
)
from homecareos.alerts.errors import EnvioError
from homecareos.alerts.router import obter_provider
from homecareos.alerts.schema import Alerta, StatusAlerta, TipoAlerta
from homecareos.alerts.service import despachar
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
)
from homecareos.db.models.enums import ResultadoValidacao
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from homecareos.rules.schema import AcaoRegra, ResultadoAvaliacao
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

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


class ProviderFake:
    """Gateway em memória: acumula o que recebeu e, se pedido, recusa o envio."""

    def __init__(self, falhar_para: set[str] | None = None) -> None:
        self.enviadas: list[tuple[str, str]] = []
        self.falhar_para = falhar_para or set()

    def enviar(self, destinatario: str, mensagem: str) -> None:
        if destinatario in self.falhar_para:
            raise EnvioError("gateway de WhatsApp recusou o envio: HTTP 401 Invalid token.")
        self.enviadas.append((destinatario, mensagem))


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
        "uazapi_base_url": BASE_URL_FALSA,
        "uazapi_token": TOKEN_FALSO,
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
    """Apaga as linhas de `alertas_enviados` deste teste, inclusive as do gancho."""
    yield
    with get_sessionmaker()() as session:
        session.execute(
            text("delete from alertas_enviados where destinatario = any(:numeros)"),
            {"numeros": list(NUMEROS_DO_TESTE)},
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
    app.dependency_overrides[obter_provider] = lambda: ProviderFake()
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
    assert "carimbo ausente" in alerta.contexto["problema"]


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


# --- envio e log --------------------------------------------------------------


def test_envio_grava_linha_enviada_com_a_mensagem_exata_que_o_gateway_recebeu(
    sessao: Session, settings: Settings, cenario: Cenario
) -> None:
    provider = ProviderFake()
    alertas = detectar_documento_incompleto_critico(
        sessao, settings, documento_id=cenario.critico.id
    )

    resumo = despachar(sessao, _com_alertas(settings), provider, alertas)

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
    alertas = [_alerta_de_teste("documento:cooldown")]
    resolvido = _com_alertas(settings)

    primeiro = despachar(sessao, resolvido, provider, alertas)
    segundo = despachar(sessao, resolvido, provider, alertas)

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
    alertas = [_alerta_de_teste("documento:primeiro"), _alerta_de_teste("documento:segundo")]

    resumo = despachar(sessao, resolvido, provider, alertas)

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

    resumo = despachar(sessao, resolvido, provider, [_alerta_de_teste("documento:falha")])

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
        sessao, _com_alertas(settings), None, [_alerta_de_teste("documento:sem-provider")]
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
    app.dependency_overrides[obter_provider] = lambda: ProviderFake()
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
    monkeypatch.setattr(hooks, "get_provider", lambda _settings: ProviderQueExplode())
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
    monkeypatch.setattr(hooks, "get_provider", lambda _settings: provider)

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
    monkeypatch.setattr(hooks, "get_provider", lambda _settings: provider)

    hooks.notificar_classificacao(cenario.critico.id)

    assert [destinatario for destinatario, _ in provider.enviadas] == [DESTINATARIO_A]
    (linha,) = _linhas(sessao, DESTINATARIO_A)
    assert linha.status == StatusAlerta.ENVIADO.value
    assert linha.documento_id == cenario.critico.id
