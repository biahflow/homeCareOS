"""Leitura e validação da configuração de alertas — canais, destinatários e templates.

## Por que o parse é preguiçoso, e não um validador de `Settings`

`ALERTAS_DESTINATARIOS`, `ALERTAS_TEMPLATES`, `ALERTAS_CANAIS` e
`ALERTAS_PAPEIS_EMAIL` são configuração **em string**, e são lidos aqui, sob
demanda — nunca num `field_validator` de `Settings`.

`Settings` é construída no import de `main.py` (`app = create_app()`). Se um
JSON malformado de alerta derrubasse a construção de `Settings`, um erro de
digitação numa configuração de **notificação** impediria a API de **receber
documento**: trocaria uma falha pequena (ninguém é avisado) por uma grande (a
conferência inteira sai do ar). Por isso o erro só nasce quando a varredura ou
o endpoint de alertas rodam, e chega a quem opera como 422 — não como um
container que não sobe.

## Chave desconhecida é erro, e não é preciosismo

Um typo em `"deadline_competencia"` que fosse ignorado em silêncio significaria
"nunca mais recebi esse alerta e não sei por quê" — o pior desfecho possível
para um sistema cujo produto é justamente avisar. Vale igual para os quatro
mapas: destinatário de tipo inexistente nunca receberia nada, template de tipo
inexistente nunca seria usado, canal com nome errado nunca enviaria e papel
inexistente nunca resolveria e-mail nenhum.

Tipo **ausente** do mapa é outra coisa, e não é erro: em `ALERTAS_DESTINATARIOS`
é o jeito de desligar um tipo de alerta no WhatsApp (sem destinatário, ninguém
é notificado); em `ALERTAS_PAPEIS_EMAIL` é o jeito de aceitar o default
declarado (ver `PAPEIS_EMAIL_PADRAO`).

## `ALERTAS_CANAIS` não decide mais nada

O liga/desliga dos canais **saiu daqui** na parte 2 do ADR 0006: ele vive na
tabela `canais_alerta`, é editável pelo coordenador e a mudança é auditada
(`alerts/canais_repository.py`). `canais_habilitados` continua existindo por um
motivo só — foi a **semente** da migration que criou aquela tabela, e é lá que
ela ainda é lida, para que quem rodava com `ALERTAS_CANAIS=whatsapp` não
mudasse de comportamento no deploy.

Por isso ela **não** entra mais em `validar`: depois desta entrega um typo em
`ALERTAS_CANAIS` não desliga canal nenhum, e derrubar a varredura por causa de
uma variável inerte trocaria uma configuração morta com erro de digitação por
uma operação sem aviso — exatamente o desfecho que este módulo inteiro existe
para evitar. Onde o typo ainda importa é na migration, e lá ele para o deploy,
que é o lugar certo para parar.

O que **não** mudou: a credencial continua no `.env`. São duas perguntas
diferentes, e as duas precisam de resposta afirmativa para um canal enviar
(`ResumoVarredura.canais`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from homecareos.alerts.errors import AlertConfigError
from homecareos.alerts.schema import Canal, TipoAlerta
from homecareos.auth.schema import Papel
from homecareos.config import Settings

# Caracteres de apresentação que uma pessoa digita ao anotar um telefone e que
# o gateway não quer ver: ele espera só dígitos (`5521999999999`).
_SEPARADORES = re.compile(r"[+\s().-]")

# 10 dígitos é o piso de um fixo nacional com DDD; 15 é o teto do E.164.
_TELEFONE = re.compile(r"^\d{10,15}$")


PAPEIS_EMAIL_PADRAO: dict[TipoAlerta, tuple[Papel, ...]] = {
    TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO: (Papel.COORDENADOR,),
    TipoAlerta.DEADLINE_COMPETENCIA: (Papel.COORDENADOR,),
    TipoAlerta.PENDENCIA_PARADA: (Papel.COORDENADOR,),
    TipoAlerta.VOLUME_ANORMAL: (Papel.COORDENADOR, Papel.GESTOR),
}
"""Qual papel recebe qual tipo por e-mail. **ASSUNÇÃO deste time, não requisito
confirmado pelo cliente** — o ADR 0006 deixa "os alertas por e-mail vão para
quais papéis?" explicitamente em aberto, como calibragem de produto. O default
existe porque um mapa vazio entregaria um canal que não notifica ninguém; ele é
sobrescrevível por `ALERTAS_PAPEIS_EMAIL` sem deploy, e some no dia em que a
conversa de produto acontecer.

Três dos quatro tipos vão só para o **coordenador** porque são item
individual — alguém precisa agir naquele documento, naquela pendência, naquela
competência. `volume_anormal` é o único sinal **agregado** dos quatro ("a taxa
de problema do dia saiu da média"), que é leitura da operação e não execução
dela, e por isso inclui o **gestor** (matriz do ADR 0001: o gestor lê a
operação, não a executa). Mandar ao gestor um aviso por documento seria
enchê-lo de item individual que ele não vai tratar — e alerta que não se trata
ensina a ignorar o canal, que é o problema que o texto do WhatsApp acabou de
atacar."""


@dataclass(frozen=True)
class OverrideTemplate:
    """As sobrescritas de template de UM tipo de alerta, uma por espaço renderizável.

    Três espaços e não dois porque o e-mail tem assunto e corpo, e eles falham
    de forma independente: um assunto customizado com typo não pode arrastar o
    corpo customizado que estava certo para o padrão junto.

    `None` em qualquer campo significa "não sobrescrito" — e não "sobrescrito
    com vazio".
    """

    whatsapp: str | None = None
    email_assunto: str | None = None
    email_corpo: str | None = None


# Os espaços renderizáveis que `ALERTAS_TEMPLATES` aceita sobrescrever na forma
# de objeto. São os nomes dos campos de `OverrideTemplate`, e a coincidência é
# proposital: o que quem opera digita é o que o código nomeia.
_ESPACOS_DE_TEMPLATE = ("whatsapp", "email_assunto", "email_corpo")


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


def canais_habilitados(settings: Settings) -> set[Canal]:
    """Canais ligados em `ALERTAS_CANAIS` (lista separada por vírgula).

    **Não é mais a fonte de verdade da aplicação.** Desde a parte 2 do ADR 0006
    o liga/desliga vem da tabela `canais_alerta`
    (`alerts.canais_repository.canais_habilitados`); esta função sobreviveu
    porque a migration que criou aquela tabela precisa de um estado inicial, e
    o valor antigo é o único default honesto para ele — ver a docstring do
    módulo e a da migration `a4d6c8b21f37`.

    Vazio devolve conjunto vazio. O default de `Settings` é `"whatsapp"`, então
    um `.env` que nunca conheceu a variável foi semeado com o comportamento de
    sempre: WhatsApp ligado, e-mail desligado.

    Habilitado não é o mesmo que envia: falta a credencial (ver
    `alerts/canais.py`).
    """
    habilitados: set[Canal] = set()
    for bruto in settings.alertas_canais.split(","):
        nome = bruto.strip()
        if not nome:
            continue
        try:
            habilitados.add(Canal(nome))
        except ValueError as exc:
            raise AlertConfigError(
                f"ALERTAS_CANAIS tem o canal desconhecido {nome!r}. "
                f"Canais válidos: {', '.join(canal.value for canal in Canal)}."
            ) from exc
    return habilitados


def papeis_por_tipo(settings: Settings) -> dict[TipoAlerta, tuple[Papel, ...]]:
    """Quais papéis recebem cada tipo por e-mail, com `PAPEIS_EMAIL_PADRAO` embaixo.

    `ALERTAS_PAPEIS_EMAIL` é sobrescrita **parcial**, e é a única variável de
    alerta que funciona assim. A razão é o custo do engano: exigir que quem
    quer mudar um tipo redeclare os quatro faz o esquecimento de um deles
    silenciar aquele alerta, que é exatamente o modo de falha que este módulo
    inteiro existe para evitar. Lista vazia continua sendo o jeito explícito de
    desligar um tipo neste canal.
    """
    bruto = _objeto_json(settings.alertas_papeis_email, variavel="ALERTAS_PAPEIS_EMAIL")
    resolvido = dict(PAPEIS_EMAIL_PADRAO)
    for chave, valor in bruto.items():
        tipo = _tipo_de(chave, variavel="ALERTAS_PAPEIS_EMAIL")
        if not isinstance(valor, list):
            raise AlertConfigError(
                f"ALERTAS_PAPEIS_EMAIL[{chave!r}] precisa ser uma lista de papéis, "
                f"e não {type(valor).__name__}"
            )
        resolvido[tipo] = tuple(_papel_de(str(papel)) for papel in valor)
    return resolvido


def _papel_de(bruto: str) -> Papel:
    try:
        return Papel(bruto)
    except ValueError as exc:
        raise AlertConfigError(
            f"ALERTAS_PAPEIS_EMAIL tem o papel desconhecido {bruto!r}. "
            f"Papéis válidos: {', '.join(papel.value for papel in Papel)}."
        ) from exc


def destinatarios(settings: Settings) -> dict[TipoAlerta, list[str]]:
    """Telefones por tipo de alerta, já normalizados. Vazio quando não configurado.

    Só o WhatsApp usa este mapa. O e-mail resolve destinatário por papel
    (`papeis_por_tipo`), e a assimetria é consequência do dado que existe:
    `usuarios` tem e-mail e não tem telefone (ADR 0006).
    """
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


def templates(settings: Settings) -> dict[TipoAlerta, OverrideTemplate]:
    """Sobrescritas de template por tipo. Vazio quando não configurado.

    Duas formas convivem, e a primeira é a que já existe em produção:

    - **texto** — sobrescreve o template de **WhatsApp**, exatamente como antes
      do ADR 0006. Nenhum override configurado hoje precisa ser reescrito.
    - **objeto** — sobrescreve espaço a espaço:
      `{"whatsapp": "...", "email_assunto": "...", "email_corpo": "..."}`.
      Chave ausente = espaço não sobrescrito.

    Chave desconhecida dentro do objeto é erro, pela mesma razão que tipo
    desconhecido é: um `"e-mail_assunto"` ignorado em silêncio viraria "o
    assunto que eu configurei nunca aparece e não sei por quê".

    O que fazer com um template sintaticamente válido mas com placeholder que o
    contexto não tem é decisão de `alerts/templates.py`, não daqui: aquilo só
    aparece na hora de renderizar.
    """
    bruto = _objeto_json(settings.alertas_templates, variavel="ALERTAS_TEMPLATES")
    resolvido: dict[TipoAlerta, OverrideTemplate] = {}
    for chave, valor in bruto.items():
        tipo = _tipo_de(chave, variavel="ALERTAS_TEMPLATES")
        resolvido[tipo] = _override_de(chave, valor)
    return resolvido


def _override_de(chave: str, valor: Any) -> OverrideTemplate:
    if isinstance(valor, str):
        return OverrideTemplate(whatsapp=valor)
    if not isinstance(valor, dict):
        raise AlertConfigError(
            f"ALERTAS_TEMPLATES[{chave!r}] precisa ser um texto (template de WhatsApp) "
            f'ou um objeto por canal ({{"whatsapp": "...", "email_assunto": "...", '
            f'"email_corpo": "..."}}), e não {type(valor).__name__}'
        )
    espacos: dict[str, str] = {}
    for espaco, texto in valor.items():
        if espaco not in _ESPACOS_DE_TEMPLATE:
            raise AlertConfigError(
                f"ALERTAS_TEMPLATES[{chave!r}] tem o espaço de template desconhecido "
                f"{espaco!r}. Espaços válidos: {', '.join(_ESPACOS_DE_TEMPLATE)}."
            )
        if not isinstance(texto, str):
            raise AlertConfigError(
                f"ALERTAS_TEMPLATES[{chave!r}][{espaco!r}] precisa ser um texto, "
                f"e não {type(texto).__name__}"
            )
        espacos[espaco] = texto
    return OverrideTemplate(**espacos)


def validar(settings: Settings) -> None:
    """Lê as configurações de alerta que ainda decidem algo, para um typo estourar cedo.

    Chamada no começo da varredura, **antes** da guarda de canal indisponível:
    um erro de digitação precisa aparecer como 422 (ou saída 1 no cron) mesmo
    em ambiente sem credencial nenhuma — senão só se descobre a configuração
    quebrada no dia em que o alerta deveria ter saído.

    `canais_habilitados` ficou **de fora** na parte 2 do ADR 0006: `ALERTAS_CANAIS`
    deixou de decidir envio, e recusar a varredura por causa dela silenciaria a
    operação por uma variável que não faz nada (ver a docstring do módulo).
    """
    destinatarios(settings)
    papeis_por_tipo(settings)
    templates(settings)
