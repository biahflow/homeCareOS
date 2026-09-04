"""Templates das mensagens de alerta, um por tipo **e por canal**.

O texto de WhatsApp é o da issue #9 (revisto depois do primeiro envio real, ver
`README.md#o-texto-do-alerta-é-escrito-para-o-whatsapp-não-para-a-tela`),
com placeholders nomeados (`{operadora}`, `{deadline}`, ...). `ALERTAS_TEMPLATES`
sobrescreve qualquer um deles sem deploy — é texto que quem opera quer ajustar
sem passar por engenharia.

## Por que o template é por canal, e não um texto neutro para os dois

Os templates de WhatsApp usam `*negrito*` e emoji. Num e-mail de texto puro —
e `mailer/smtp.py` manda texto puro, decisão deliberada da issue #34 — os
asteriscos apareceriam **literais**. A saída errada seria tirar a marcação dos
dois: ela existe porque no WhatsApp funciona, e apagá-la pioraria o canal que
hoje é o único que roda (ADR 0006).

Além da marcação, o e-mail tem um espaço que o WhatsApp não tem: o **assunto**.
Ele decide se a pessoa abre — "Pendência crítica — Operadora Ciclo" diz o que
"Alerta do HomeCareOS" não diz —, e é por isso que `renderizar` devolve
`MensagemAlerta` (assunto + corpo) e não mais uma string.

## Template customizado com erro nunca cala o alerta

A regra que governa este módulo: **o alerta é a coisa que existe justamente
para não ser perdida.** Um `{nao_existe}` digitado no override não pode virar
uma exceção que derruba a varredura nem um envio que não acontece. Ele vira um
`logger.warning` nomeando o tipo, o espaço e o placeholder — para alguém
consertar — e o template padrão é usado no lugar.

Os três espaços (texto do WhatsApp, assunto do e-mail, corpo do e-mail) falham
**separadamente**: um assunto customizado com typo não arrasta para o padrão o
corpo customizado que estava certo.

Pela mesma razão o padrão renderiza com um mapa tolerante: placeholder que o
contexto não trouxe vira `"não informado"`, nunca `"None"` e nunca um
`KeyError` que faria a mensagem inteira desaparecer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homecareos.alerts import config
from homecareos.alerts.schema import Canal, MensagemAlerta, TipoAlerta
from homecareos.config import Settings

logger = logging.getLogger(__name__)

VALOR_AUSENTE = "não informado"

TEMPLATES_PADRAO: dict[TipoAlerta, str] = {
    TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO: (
        "🚨 *Pendência crítica*\n"
        "{linha_paciente}"
        "Operadora: {operadora}\n"
        "Prazo: {deadline}\n"
        "\n"
        "Faltando:\n"
        "{problema}\n"
        "\n"
        "{acao}"
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
        "{linha_paciente}"
        "Operadora: {operadora}\n"
        "Problema: {problema}\n"
        "Aberta há {horas}h sem ação.\n"
        "Prazo: {deadline}"
    ),
}
"""Templates de WhatsApp. **Não mexer no texto sem motivo**: ele foi corrigido
depois do primeiro envio real (lista com marcador, rótulo humano no lugar do
nome técnico do campo, "Prazo" no lugar de "Deadline"), e regredir qualquer um
desses pontos desfaz uma correção que custou uma mensagem ilegível em
produção."""


@dataclass(frozen=True)
class TemplateEmail:
    """Os dois espaços de um e-mail. Texto puro nos dois — nada de `*negrito*`."""

    assunto: str
    corpo: str


TEMPLATES_EMAIL_PADRAO: dict[TipoAlerta, TemplateEmail] = {
    TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO: TemplateEmail(
        assunto="Pendência crítica — {operadora}",
        corpo=(
            "{linha_paciente}"
            "Operadora: {operadora}\n"
            "Prazo: {deadline}\n"
            "\n"
            "Faltando:\n"
            "{problema}\n"
            "\n"
            "{acao}\n"
        ),
    ),
    TipoAlerta.DEADLINE_COMPETENCIA: TemplateEmail(
        assunto="Prazo de competência {competencia} — {operadora}",
        corpo=(
            "Operadora: {operadora}\n"
            "Competência: {competencia}\n"
            "Documentos com pendência: {documentos}\n"
            "\n"
            "Faltam {dias} dia(s) para o envio ({deadline}).\n"
        ),
    ),
    TipoAlerta.VOLUME_ANORMAL: TemplateEmail(
        assunto="Volume anormal de problemas em {data}",
        corpo=(
            "Data: {data}\n"
            "Documentos do dia: {documentos}\n"
            "Taxa de problema hoje: {taxa_hoje}\n"
            "Média dos últimos {janela} dias: {taxa_media}\n"
            "\n"
            "Vale conferir se há erro sistêmico no campo.\n"
        ),
    ),
    TipoAlerta.PENDENCIA_PARADA: TemplateEmail(
        assunto="Pendência parada há {horas}h — {operadora}",
        corpo=(
            "{linha_paciente}"
            "Operadora: {operadora}\n"
            "Problema: {problema}\n"
            "Prazo: {deadline}\n"
            "\n"
            "Aberta há {horas}h sem ação.\n"
        ),
    ),
}
"""Templates de e-mail. Os assuntos nomeiam **o que aconteceu e onde**, e não a
origem da mensagem: quem recebe já sabe que o remetente é o HomeCareOS, e um
assunto que só repete isso não ajuda a decidir se vale abrir agora."""


_FALLBACK_DE_LINHA: dict[str, str] = {"linha_paciente": ""}
"""Placeholders de **linha inteira** (rótulo + valor + quebra de linha, ou nada)
não podem cair no fallback `"não informado"` de `_ContextoTolerante`: colaria o
texto sem quebra de linha na linha seguinte (`"não informadoOperadora: ..."`).
Ausente daqui, um `{linha_x}` que falte cai no fallback igual a qualquer outro —
esta é uma exceção nomeada, não um mecanismo genérico."""


class _ContextoTolerante(dict[str, str]):
    """Contexto que responde `"não informado"` a placeholder que não conhece.

    Usado só no template padrão, e é o que garante que um detector que deixou de
    preencher um campo produza uma mensagem incompleta — mas produza — em vez de
    nenhuma mensagem.
    """

    def __missing__(self, chave: str) -> str:
        return _FALLBACK_DE_LINHA.get(chave, VALOR_AUSENTE)


def renderizar(
    canal: Canal, tipo: TipoAlerta, contexto: dict[str, str], settings: Settings
) -> MensagemAlerta:
    """Renderiza o alerta para um canal, com fallback para o template padrão."""
    override = config.templates(settings).get(tipo)
    if canal is Canal.WHATSAPP:
        return MensagemAlerta(
            corpo=_renderizar_espaco(
                TEMPLATES_PADRAO[tipo],
                override.whatsapp if override is not None else None,
                contexto,
                tipo=tipo,
                espaco="whatsapp",
            )
        )

    padrao = TEMPLATES_EMAIL_PADRAO[tipo]
    assunto = _renderizar_espaco(
        padrao.assunto,
        override.email_assunto if override is not None else None,
        contexto,
        tipo=tipo,
        espaco="email_assunto",
    )
    corpo = _renderizar_espaco(
        padrao.corpo,
        override.email_corpo if override is not None else None,
        contexto,
        tipo=tipo,
        espaco="email_corpo",
    )
    return MensagemAlerta(assunto=_uma_linha(assunto), corpo=corpo)


def _renderizar_espaco(
    padrao: str,
    override: str | None,
    contexto: dict[str, str],
    *,
    tipo: TipoAlerta,
    espaco: str,
) -> str:
    """Um espaço renderizável: tenta o override, cai no padrão sem nunca levantar."""
    if override is not None:
        try:
            return override.format_map(contexto)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "template customizado de %s (%s) ignorado (%s: %s); usando o template padrão. "
                "Placeholders disponíveis: %s",
                tipo.value,
                espaco,
                type(exc).__name__,
                exc,
                ", ".join(sorted(contexto)),
            )
    return padrao.format_map(_ContextoTolerante(contexto))


def _uma_linha(assunto: str) -> str:
    """Colapsa o assunto numa linha só — e isso é segurança, não estética.

    Um `\\n` num header de e-mail é injeção de cabeçalho: o que vem depois da
    quebra vira outro header (`Bcc:`, por exemplo). O texto pode chegar aqui
    com quebra por duas portas — um `ALERTAS_TEMPLATES` customizado e um valor
    do contexto, como o nome de uma operadora cadastrada com quebra de linha —,
    e nenhuma das duas é confiável o bastante para ir direto para o
    `EmailMessage`.
    """
    return " ".join(assunto.split())
