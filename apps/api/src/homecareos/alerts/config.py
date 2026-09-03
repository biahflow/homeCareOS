"""Leitura e validação da configuração de alertas — destinatários e templates.

## Por que o parse é preguiçoso, e não um validador de `Settings`

`ALERTAS_DESTINATARIOS` e `ALERTAS_TEMPLATES` são JSON **em string**, e são
lidos aqui, sob demanda — nunca num `field_validator` de `Settings`.

`Settings` é construída no import de `main.py` (`app = create_app()`). Se um
JSON malformado de alerta derrubasse a construção de `Settings`, um erro de
digitação numa configuração de **notificação** impediria a API de **receber
documento**: trocaria uma falha pequena (ninguém é avisado no WhatsApp) por uma
grande (a conferência inteira sai do ar). Por isso o erro só nasce quando a
varredura ou o endpoint de alertas rodam, e chega a quem opera como 422 — não
como um container que não sobe.

## Chave desconhecida é erro, e não é preciosismo

Um typo em `"deadline_competencia"` que fosse ignorado em silêncio significaria
"nunca mais recebi esse alerta e não sei por quê" — o pior desfecho possível
para um sistema cujo produto é justamente avisar. Vale igual para os dois
mapas: destinatário de tipo inexistente nunca receberia nada, e template de
tipo inexistente nunca seria usado.

Tipo **ausente** do mapa é outra coisa, e não é erro: é o jeito de desligar um
tipo de alerta (sem destinatário, ninguém é notificado).
"""

from __future__ import annotations

import json
import re
from typing import Any

from homecareos.alerts.errors import AlertConfigError
from homecareos.alerts.schema import TipoAlerta
from homecareos.config import Settings

# Caracteres de apresentação que uma pessoa digita ao anotar um telefone e que
# o gateway não quer ver: ele espera só dígitos (`5521999999999`).
_SEPARADORES = re.compile(r"[+\s().-]")

# 10 dígitos é o piso de um fixo nacional com DDD; 15 é o teto do E.164.
_TELEFONE = re.compile(r"^\d{10,15}$")


def _tipos_validos() -> str:
    return ", ".join(tipo.value for tipo in TipoAlerta)


def _objeto_json(bruto: str, *, variavel: str) -> dict[str, Any]:
    """Interpreta a string de configuração como um objeto JSON. Vazio vira `{}`."""
    if not bruto.strip():
        return {}
    try:
        carregado = json.loads(bruto)
    except json.JSONDecodeError as exc:
        raise AlertConfigError(f"{variavel} não é JSON válido: {exc}") from exc
    if not isinstance(carregado, dict):
        raise AlertConfigError(
            f'{variavel} precisa ser um objeto JSON ({{"<tipo>": ...}}), '
            f"e não {type(carregado).__name__}"
        )
    return carregado


def _tipo_de(chave: str, *, variavel: str) -> TipoAlerta:
    try:
        return TipoAlerta(chave)
    except ValueError as exc:
        raise AlertConfigError(
            f"{variavel} tem o tipo de alerta desconhecido {chave!r}. "
            f"Tipos válidos: {_tipos_validos()}."
        ) from exc


def normalizar_telefone(bruto: str) -> str:
    """`"+55 (21) 99999-9999"` -> `"5521999999999"`.

    O valor inteiro aparece na mensagem de erro de propósito: é um telefone da
    própria empresa, escrito por quem configurou o sistema, e mostrar só um
    pedaço obrigaria a pessoa a adivinhar qual das linhas do JSON está errada.
    Segredo de terceiro (o token do gateway) segue outra regra — ver
    `alerts/uazapi.py`.
    """
    digitos = _SEPARADORES.sub("", bruto)
    if not _TELEFONE.match(digitos):
        raise AlertConfigError(
            f"telefone inválido em ALERTAS_DESTINATARIOS: {bruto!r}. "
            "Use DDI + DDD + número, só dígitos (ex.: 5521999999999)."
        )
    return digitos


def destinatarios(settings: Settings) -> dict[TipoAlerta, list[str]]:
    """Telefones por tipo de alerta, já normalizados. Vazio quando não configurado."""
    bruto = _objeto_json(settings.alertas_destinatarios, variavel="ALERTAS_DESTINATARIOS")
    resolvido: dict[TipoAlerta, list[str]] = {}
    for chave, valor in bruto.items():
        tipo = _tipo_de(chave, variavel="ALERTAS_DESTINATARIOS")
        if not isinstance(valor, list):
            raise AlertConfigError(
                f"ALERTAS_DESTINATARIOS[{chave!r}] precisa ser uma lista de telefones, "
                f"e não {type(valor).__name__}"
            )
        resolvido[tipo] = [normalizar_telefone(str(numero)) for numero in valor]
    return resolvido


def templates(settings: Settings) -> dict[TipoAlerta, str]:
    """Sobrescritas de template por tipo. Vazio quando não configurado.

    O que fazer com um template sintaticamente válido mas com placeholder que o
    contexto não tem é decisão de `alerts/templates.py`, não daqui: aquilo só
    aparece na hora de renderizar.
    """
    bruto = _objeto_json(settings.alertas_templates, variavel="ALERTAS_TEMPLATES")
    resolvido: dict[TipoAlerta, str] = {}
    for chave, valor in bruto.items():
        tipo = _tipo_de(chave, variavel="ALERTAS_TEMPLATES")
        if not isinstance(valor, str):
            raise AlertConfigError(
                f"ALERTAS_TEMPLATES[{chave!r}] precisa ser um texto, e não {type(valor).__name__}"
            )
        resolvido[tipo] = valor
    return resolvido
