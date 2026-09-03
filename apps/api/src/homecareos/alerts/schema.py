"""Vocabulário dos alertas de WhatsApp — issue #9.

`TipoAlerta` é a chave que amarra as quatro pontas soltas do módulo: o detector
que produz o alerta, o template que o escreve, o destinatário configurado que o
recebe e a linha de `alertas_enviados` que registra o envio. Trocar o valor de
um membro aqui invalida configuração de produção (`ALERTAS_DESTINATARIOS` é
JSON com estes nomes) e não casa mais com o histórico já gravado — é migration
de dado, não renomeação.
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

    provider_configurado: bool = False
    """`False` quando o gateway não está configurado. Distingue 'não havia o que
    enviar' de 'não havia como enviar' — `enviados == 0` sozinho é ambíguo."""
