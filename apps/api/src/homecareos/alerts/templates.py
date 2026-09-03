"""Templates das mensagens de WhatsApp, um por tipo de alerta.

O texto padrão é o da issue #9, com placeholders nomeados (`{paciente}`,
`{deadline}`, ...). `ALERTAS_TEMPLATES` sobrescreve qualquer um deles sem
deploy — é texto que quem opera quer ajustar ("Ação necessária" vira "O que
fazer") sem passar por engenharia.

## Template customizado com erro nunca cala o alerta

A regra que governa este módulo: **o alerta é a coisa que existe justamente
para não ser perdida.** Um `{nao_existe}` digitado no override não pode virar
uma exceção que derruba a varredura nem um envio que não acontece. Ele vira um
`logger.warning` nomeando o tipo e o placeholder — para alguém consertar — e o
template padrão é usado no lugar.

Pela mesma razão o padrão renderiza com um mapa tolerante: placeholder que o
contexto não trouxe vira `"não informado"`, nunca `"None"` e nunca um
`KeyError` que faria a mensagem inteira desaparecer.
"""

from __future__ import annotations

import logging

from homecareos.alerts import config
from homecareos.alerts.schema import TipoAlerta
from homecareos.config import Settings

logger = logging.getLogger(__name__)

VALOR_AUSENTE = "não informado"

TEMPLATES_PADRAO: dict[TipoAlerta, str] = {
    TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO: (
        "🚨 *Pendência crítica*\n"
        "Paciente: {paciente}\n"
        "Operadora: {operadora}\n"
        "Problema: {problema}\n"
        "Deadline: {deadline}\n"
        "Ação necessária: {acao}"
    ),
    TipoAlerta.DEADLINE_COMPETENCIA: (
        "⏳ *Prazo de competência*\n"
        "Operadora: {operadora}\n"
        "Competência: {competencia}\n"
        "Documentos com pendência: {documentos}\n"
        "Faltam {dias} dia(s) para o envio ({deadline})."
    ),
    TipoAlerta.VOLUME_ANORMAL: (
        "📈 *Volume anormal de problemas*\n"
        "Data: {data}\n"
        "Documentos do dia: {documentos}\n"
        "Taxa de problema hoje: {taxa_hoje}\n"
        "Média dos últimos {janela} dias: {taxa_media}\n"
        "Vale conferir se há erro sistêmico no campo."
    ),
    TipoAlerta.PENDENCIA_PARADA: (
        "⌛ *Pendência parada*\n"
        "Paciente: {paciente}\n"
        "Operadora: {operadora}\n"
        "Problema: {problema}\n"
        "Aberta há {horas}h sem ação.\n"
        "Deadline: {deadline}"
    ),
}


class _ContextoTolerante(dict[str, str]):
    """Contexto que responde `"não informado"` a placeholder que não conhece.

    Usado só no template padrão, e é o que garante que um detector que deixou de
    preencher um campo produza uma mensagem incompleta — mas produza — em vez de
    nenhuma mensagem.
    """

    def __missing__(self, chave: str) -> str:
        return VALOR_AUSENTE


def renderizar(tipo: TipoAlerta, contexto: dict[str, str], settings: Settings) -> str:
    """Renderiza a mensagem do alerta, com fallback para o template padrão."""
    padrao = TEMPLATES_PADRAO[tipo]
    override = config.templates(settings).get(tipo)
    if override is not None:
        try:
            return override.format_map(contexto)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "template customizado de %s ignorado (%s: %s); usando o template padrão. "
                "Placeholders disponíveis: %s",
                tipo.value,
                type(exc).__name__,
                exc,
                ", ".join(sorted(contexto)),
            )
    return padrao.format_map(_ContextoTolerante(contexto))
