"""Vocabulário dos alertas — issue #9, estendido pelo ADR 0006 (canais).

`TipoAlerta` é a chave que amarra as quatro pontas soltas do módulo: o detector
que produz o alerta, o template que o escreve, o destinatário configurado que o
recebe e a linha de `alertas_enviados` que registra o envio. Trocar o valor de
um membro aqui invalida configuração de produção (`ALERTAS_DESTINATARIOS` é
JSON com estes nomes) e não casa mais com o histórico já gravado — é migration
de dado, não renomeação.

`Canal` é a segunda chave, criada pelo ADR 0006: um alerta é sempre renderizado
e entregue POR UM CANAL, e a linha do log diz por qual. Vale a mesma regra de
estabilidade, e ela ficou mais dura na parte 2 do ADR: o valor vai para
`alertas_enviados.canal`, para `canais_alerta.canal` e
`auditoria_canais_alerta.canal` (o estado configurado e o histórico dele), para
o path de `PATCH /api/alertas/canais/{canal}` e para a semente
`ALERTAS_CANAIS`. Renomear um membro aqui é migração de dado em três tabelas e
quebra de contrato de API — não renomeação.
"""

from __future__ import annotations

import enum
import uuid

from pydantic import BaseModel, Field


class TipoAlerta(enum.StrEnum):
    """Os quatro alertas que a issue #9 pede."""

    DOCUMENTO_INCOMPLETO_CRITICO = "documento_incompleto_critico"
    DEADLINE_COMPETENCIA = "deadline_competencia"
    VOLUME_ANORMAL = "volume_anormal"
    PENDENCIA_PARADA = "pendencia_parada"


class Canal(enum.StrEnum):
    """Por onde o alerta sai (ADR 0006).

    Os dois canais são **independentes**, nunca reserva um do outro: quem
    estiver nos dois recebe o mesmo aviso duas vezes, e isso é o comportamento
    desejado. Fallback (e-mail quando o WhatsApp falha) foi descartado pelo ADR
    — exigiria saber que o envio falhou de verdade, e o gateway aceitar a
    mensagem não prova entrega.
    """

    WHATSAPP = "whatsapp"
    EMAIL = "email"


class StatusAlerta(enum.StrEnum):
    """Desfecho de uma tentativa de notificação, gravado em `alertas_enviados.status`.

    `SUPRIMIDO` só aparece na supressão por rate limit. A supressão por cooldown
    não grava linha nenhuma — ver a justificativa em `alerts/service.py`.
    """

    ENVIADO = "enviado"
    FALHA = "falha"
    SUPRIMIDO = "suprimido"


class Alerta(BaseModel):
    """Um alerta detectado, ainda não renderizado nem enviado."""

    tipo: TipoAlerta
    chave: str
    """Identidade do **assunto** do alerta (`documento:<id>`, `volume:<data>`,
    ...), não da detecção: duas varreduras seguidas sobre o mesmo problema
    produzem a mesma `chave`, e é isso que permite ao cooldown reconhecer que o
    aviso já saiu."""

    contexto: dict[str, str]
    """Placeholders do template, sempre já como texto formatado — o detector é
    quem sabe formatar data, contagem e percentual do seu próprio alerta."""

    documento_id: uuid.UUID | None = None


class MensagemAlerta(BaseModel):
    """Um alerta já renderizado **para um canal**.

    `assunto` é `None` no WhatsApp, que não tem esse conceito, e obrigatório no
    e-mail: é o que decide se a pessoa abre a mensagem. Uma string só não serve
    aos dois canais, e foi por isso que `templates.renderizar` deixou de
    devolver `str` (ADR 0006).
    """

    assunto: str | None = None
    corpo: str

    def para_registro(self) -> str:
        """O texto que vai para `alertas_enviados.mensagem`.

        Auditar um envio é saber o que foi dito, e no e-mail o assunto **é**
        parte do que foi dito — omiti-lo deixaria o log respondendo pela
        metade. No WhatsApp o registro continua sendo exatamente o texto que o
        gateway recebeu, byte a byte.
        """
        if self.assunto is None:
            return self.corpo
        return f"Assunto: {self.assunto}\n\n{self.corpo}"


class Destinatario(BaseModel):
    """Para onde o alerta vai, e — quando o sistema sabe — de quem é esse endereço.

    `usuario_id` é o que o rate limit conta (ADR 0006): sem ele, o telefone e o
    e-mail da mesma pessoa seriam destinatários não relacionados e o teto por
    hora dobraria sem ninguém pedir. É `None` no telefone avulso de
    `ALERTAS_DESTINATARIOS`, que não tem vínculo com pessoa nenhuma — não há
    telefone em `usuarios` —, e nesse caso o endereço volta a ser a chave.
    """

    endereco: str
    usuario_id: uuid.UUID | None = None

    model_config = {"frozen": True}


class EstadoCanal(BaseModel):
    """As duas perguntas que decidem se um canal envia, respondidas separadamente.

    `canal habilitado (configuração) x credencial presente (.env) = canal
    envia` — ADR 0006. Mantê-las separadas é o ponto: hoje "desligado porque
    decidi" e "desligado porque não configurei" são indistinguíveis, e quem
    liga um canal sem credencial fica sem entender por que nada sai.
    """

    habilitado: bool
    disponivel: bool


class ResumoVarredura(BaseModel):
    """O que uma varredura fez, na forma que o cron e o endpoint devolvem."""

    detectados: int = 0
    enviados: int = 0
    suprimidos: int = 0
    falhas: int = 0
    por_tipo: dict[str, int] = Field(default_factory=dict)
    """Detectados por tipo. Sempre com os quatro tipos, inclusive os zerados:
    'nenhum alerta de volume anormal hoje' é informação, e um dicionário que
    muda de forma conforme o dia é ruim de acompanhar."""

    canais: dict[str, EstadoCanal] = Field(default_factory=dict)
    """Estado de **todos** os canais, sempre com todos eles (mesma razão de
    `por_tipo`): um canal que sumiu do resumo é indistinguível de um canal que
    ninguém olhou. Substitui `provider_configurado` como fonte de verdade — ver
    abaixo."""

    provider_configurado: bool = False
    """`True` quando o canal de **WhatsApp** está habilitado e com credencial.

    Mantido com o significado exato que sempre teve (o gateway de WhatsApp está
    configurado?), e não generalizado para "algum canal envia": quem consome
    este campo hoje — cron e endpoint de varredura — o lê como resposta sobre o
    WhatsApp, e alargar o sentido em silêncio faria o campo mentir de um jeito
    difícil de perceber. Com dois canais um booleano não dá conta da pergunta;
    a resposta completa é `canais`."""
