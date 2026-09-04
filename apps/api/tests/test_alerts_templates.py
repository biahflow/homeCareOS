"""Testes unitários dos templates e da configuração de alertas (issue #9, ADR 0006).

Sem banco e sem rede: tudo aqui é função pura sobre `Settings` construída à mão.
**Nada é enviado** — nem e-mail, nem WhatsApp: este módulo só renderiza texto.
"""

from __future__ import annotations

import json

import pytest

from homecareos.alerts import config, templates
from homecareos.alerts.errors import AlertConfigError
from homecareos.alerts.schema import Canal, TipoAlerta
from homecareos.auth.schema import Papel
from homecareos.config import Settings

CONTEXTOS: dict[TipoAlerta, dict[str, str]] = {
    TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO: {
        "paciente": "Maria de Souza",
        "linha_paciente": "Paciente: Maria de Souza\n",
        "operadora": "Unimed",
        "problema": "• Carimbo",
        "deadline": "10/04/2099",
        "acao": "Reenviar a evolução com carimbo e assinatura.",
    },
    TipoAlerta.DEADLINE_COMPETENCIA: {
        "operadora": "Amil",
        "competencia": "2099-03",
        "documentos": "12",
        "dias": "2",
        "deadline": "10/04/2099",
    },
    TipoAlerta.VOLUME_ANORMAL: {
        "data": "03/09/2026",
        "documentos": "40",
        "taxa_hoje": "42.9%",
        "janela": "14",
        "taxa_media": "10.0%",
    },
    TipoAlerta.PENDENCIA_PARADA: {
        "paciente": "João Pereira",
        "linha_paciente": "Paciente: João Pereira\n",
        "operadora": "Caberj",
        "problema": "Assinatura do profissional",
        "horas": "72",
        "deadline": "10/04/2099",
    },
}


def _settings(**overrides: str) -> Settings:
    return Settings(**overrides)


def _whatsapp(tipo: TipoAlerta, contexto: dict[str, str], settings: Settings) -> str:
    return templates.renderizar(Canal.WHATSAPP, tipo, contexto, settings).corpo


# --- templates ----------------------------------------------------------------


@pytest.mark.parametrize("tipo", list(TipoAlerta))
def test_template_padrao_renderiza_com_contexto_completo(tipo: TipoAlerta) -> None:
    contexto = CONTEXTOS[tipo]

    mensagem = _whatsapp(tipo, contexto, _settings())

    for valor in contexto.values():
        assert valor in mensagem
    assert "{" not in mensagem


def test_override_valido_substitui_o_template_padrao() -> None:
    settings = _settings(
        alertas_templates=json.dumps(
            {TipoAlerta.PENDENCIA_PARADA.value: "Parada há {horas}h: {problema}"}
        )
    )

    mensagem = _whatsapp(
        TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings
    )

    assert mensagem == "Parada há 72h: Assinatura do profissional"


def test_override_com_placeholder_inexistente_cai_para_o_padrao_sem_levantar() -> None:
    """Template customizado com typo não pode calar o alerta."""
    settings = _settings(
        alertas_templates=json.dumps({TipoAlerta.PENDENCIA_PARADA.value: "Parada: {nao_existe}"})
    )

    mensagem = _whatsapp(
        TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings
    )

    assert mensagem == templates.TEMPLATES_PADRAO[TipoAlerta.PENDENCIA_PARADA].format_map(
        CONTEXTOS[TipoAlerta.PENDENCIA_PARADA]
    )
    assert "nao_existe" not in mensagem


def test_valor_ausente_no_contexto_nunca_vira_none_na_mensagem() -> None:
    contexto = dict(CONTEXTOS[TipoAlerta.PENDENCIA_PARADA])
    del contexto["operadora"]

    mensagem = _whatsapp(TipoAlerta.PENDENCIA_PARADA, contexto, _settings())

    assert f"Operadora: {templates.VALOR_AUSENTE}" in mensagem
    assert "None" not in mensagem


def test_linha_paciente_vazia_omite_a_linha_inteira() -> None:
    """ "Paciente: não informado" era ruído sem paciente algum — a linha some,
    em vez de o placeholder virar "não informado"."""
    contexto = dict(CONTEXTOS[TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO])
    contexto["linha_paciente"] = ""

    mensagem = _whatsapp(TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO, contexto, _settings())

    assert "Paciente" not in mensagem


@pytest.mark.parametrize(
    "tipo", [TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO, TipoAlerta.PENDENCIA_PARADA]
)
def test_templates_dizem_prazo_e_nao_deadline(tipo: TipoAlerta) -> None:
    mensagem = _whatsapp(tipo, CONTEXTOS[tipo], _settings())

    assert "Prazo" in mensagem
    assert "Deadline" not in mensagem


def test_templates_com_json_malformado_levanta_alert_config_error() -> None:
    settings = _settings(alertas_templates="{não é json}")

    with pytest.raises(AlertConfigError) as erro:
        _whatsapp(TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings)

    assert "ALERTAS_TEMPLATES" in str(erro.value)


# --- destinatários ------------------------------------------------------------


def test_destinatarios_normaliza_os_tipos_e_os_telefones() -> None:
    settings = _settings(
        alertas_destinatarios=json.dumps(
            {
                TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO.value: ["+55 (21) 99999-9999"],
                TipoAlerta.VOLUME_ANORMAL.value: ["5511988887777"],
            }
        )
    )

    resolvido = config.destinatarios(settings)

    assert resolvido == {
        TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO: ["5521999999999"],
        TipoAlerta.VOLUME_ANORMAL: ["5511988887777"],
    }


def test_destinatarios_com_tipo_desconhecido_levanta_citando_os_tipos_validos() -> None:
    """Um typo ignorado em silêncio vira 'nunca mais recebi esse alerta e não sei por quê'."""
    settings = _settings(
        alertas_destinatarios=json.dumps({"deadline_competencias": ["5521999999999"]})
    )

    with pytest.raises(AlertConfigError) as erro:
        config.destinatarios(settings)

    mensagem = str(erro.value)
    assert "deadline_competencias" in mensagem
    for tipo in TipoAlerta:
        assert tipo.value in mensagem


def test_destinatarios_vazio_devolve_dicionario_vazio() -> None:
    assert config.destinatarios(_settings(alertas_destinatarios="")) == {}


def test_destinatarios_com_json_malformado_levanta_alert_config_error() -> None:
    with pytest.raises(AlertConfigError) as erro:
        config.destinatarios(_settings(alertas_destinatarios="[1, 2"))

    assert "ALERTAS_DESTINATARIOS" in str(erro.value)


# --- telefone -----------------------------------------------------------------


def test_normalizar_telefone_remove_pontuacao_de_apresentacao() -> None:
    assert config.normalizar_telefone("+55 (21) 99999-9999") == "5521999999999"


@pytest.mark.parametrize("bruto", ["abc", "123"])
def test_normalizar_telefone_recusa_valor_impossivel(bruto: str) -> None:
    with pytest.raises(AlertConfigError) as erro:
        config.normalizar_telefone(bruto)

    # O valor inteiro aparece: é telefone da própria empresa, não segredo de
    # terceiro, e sem ele quem configurou não acha a linha errada do JSON.
    assert bruto in str(erro.value)


# --- o texto de WhatsApp não pode regredir --------------------------------------

# Cópia LITERAL dos quatro templates de WhatsApp como estavam antes do ADR 0006
# (revision `8c4e77e`), depois da correção que a entrega anterior fez no texto:
# lista com marcador `•` em vez de `" | "`, rótulo humano em vez do nome técnico
# do campo, "Prazo" em vez de "Deadline" e a linha de paciente condicional.
#
# Este bloco existe para ser CHATO de mudar. Ele não testa comportamento: testa
# que ninguém reescreveu, ao acrescentar o canal de e-mail, o texto do canal que
# hoje é o único que roda em produção — e que custou uma mensagem ilegível para
# ficar como está.
TEXTO_WHATSAPP_ANTES_DO_ADR_0006: dict[TipoAlerta, str] = {
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


def test_o_texto_de_whatsapp_e_identico_ao_de_antes_do_segundo_canal() -> None:
    assert templates.TEMPLATES_PADRAO == TEXTO_WHATSAPP_ANTES_DO_ADR_0006


@pytest.mark.parametrize("tipo", list(TipoAlerta))
def test_whatsapp_renderizado_continua_identico_byte_a_byte(tipo: TipoAlerta) -> None:
    esperado = TEXTO_WHATSAPP_ANTES_DO_ADR_0006[tipo].format_map(CONTEXTOS[tipo])

    assert _whatsapp(tipo, CONTEXTOS[tipo], _settings()) == esperado


def test_a_lista_com_marcador_do_alerta_critico_sobrevive_ao_canal_novo() -> None:
    """A correção mais cara da entrega anterior: três pendências em três linhas
    com marcador, sem o nome técnico do campo e sem `" | "` grudando tudo."""
    contexto = dict(CONTEXTOS[TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO])
    contexto["problema"] = "• Assinatura do profissional\n• Carimbo\n• Carimbo legível"

    mensagem = _whatsapp(TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO, contexto, _settings())

    assert "• Assinatura do profissional\n• Carimbo\n• Carimbo legível" in mensagem
    assert " | " not in mensagem
    assert "carimbo_presente" not in mensagem


# --- e-mail: texto puro, com assunto -------------------------------------------


def _email(tipo: TipoAlerta, contexto: dict[str, str], settings: Settings) -> tuple[str, str]:
    mensagem = templates.renderizar(Canal.EMAIL, tipo, contexto, settings)
    assert mensagem.assunto is not None
    return mensagem.assunto, mensagem.corpo


@pytest.mark.parametrize("tipo", list(TipoAlerta))
def test_email_nao_leva_asterisco_literal_nem_emoji_de_whatsapp(tipo: TipoAlerta) -> None:
    """`*negrito*` é marcação do WhatsApp; num e-mail de texto puro (o único que
    `mailer/smtp.py` manda) os asteriscos apareceriam literais."""
    assunto, corpo = _email(tipo, CONTEXTOS[tipo], _settings())

    assert "*" not in assunto
    assert "*" not in corpo
    for emoji in ("🚨", "⏳", "📈", "⌛"):
        assert emoji not in assunto
        assert emoji not in corpo


@pytest.mark.parametrize("tipo", list(TipoAlerta))
def test_email_tem_assunto_proprio_que_diz_o_que_aconteceu(tipo: TipoAlerta) -> None:
    """O assunto decide se a pessoa abre: ele nomeia o evento, não o remetente."""
    assunto, _ = _email(tipo, CONTEXTOS[tipo], _settings())

    assert assunto.strip() != ""
    assert "{" not in assunto
    assert "HomeCareOS" not in assunto


def test_assunto_do_alerta_critico_nomeia_a_operadora() -> None:
    assunto, _ = _email(
        TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO,
        CONTEXTOS[TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO],
        _settings(),
    )

    assert assunto == "Pendência crítica — Unimed"


@pytest.mark.parametrize("tipo", list(TipoAlerta))
def test_email_renderiza_o_contexto_inteiro_sem_placeholder_solto(tipo: TipoAlerta) -> None:
    _, corpo = _email(tipo, CONTEXTOS[tipo], _settings())

    assert "{" not in corpo


def test_assunto_nunca_tem_quebra_de_linha_mesmo_com_valor_sujo() -> None:
    """Quebra de linha em header de e-mail é injeção de cabeçalho: o que vem
    depois da quebra viraria outro header. O valor pode chegar do cadastro."""
    contexto = dict(CONTEXTOS[TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO])
    contexto["operadora"] = "Unimed\nBcc: intruso@exemplo.com"

    assunto, _ = _email(TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO, contexto, _settings())

    assert "\n" not in assunto
    assert "\r" not in assunto


def test_email_tolera_placeholder_ausente_como_o_whatsapp() -> None:
    contexto = dict(CONTEXTOS[TipoAlerta.PENDENCIA_PARADA])
    del contexto["operadora"]

    _, corpo = _email(TipoAlerta.PENDENCIA_PARADA, contexto, _settings())

    assert f"Operadora: {templates.VALOR_AUSENTE}" in corpo
    assert "None" not in corpo


# --- ALERTAS_TEMPLATES com dois canais -----------------------------------------


def test_override_em_texto_continua_sendo_o_do_whatsapp() -> None:
    """Compatibilidade: o override configurado hoje em produção é uma string, e
    é de WhatsApp. Ele não pode virar template de e-mail nem parar de valer."""
    settings = _settings(
        alertas_templates=json.dumps(
            {TipoAlerta.PENDENCIA_PARADA.value: "Parada há {horas}h: {problema}"}
        )
    )

    assert (
        _whatsapp(TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings)
        == "Parada há 72h: Assinatura do profissional"
    )
    # E o e-mail segue no padrão dele, sem herdar a string de WhatsApp.
    assunto, corpo = _email(
        TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings
    )
    assert assunto == "Pendência parada há 72h — Caberj"
    assert corpo.startswith("Paciente: João Pereira\n")


def test_override_por_espaco_sobrescreve_so_o_que_foi_dito() -> None:
    settings = _settings(
        alertas_templates=json.dumps(
            {
                TipoAlerta.PENDENCIA_PARADA.value: {
                    "email_assunto": "[URGENTE] {operadora} parada há {horas}h"
                }
            }
        )
    )

    assunto, corpo = _email(
        TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings
    )

    assert assunto == "[URGENTE] Caberj parada há 72h"
    # Corpo e WhatsApp não foram tocados.
    assert "Problema: Assinatura do profissional" in corpo
    assert _whatsapp(
        TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings
    ) == templates.TEMPLATES_PADRAO[TipoAlerta.PENDENCIA_PARADA].format_map(
        CONTEXTOS[TipoAlerta.PENDENCIA_PARADA]
    )


def test_assunto_customizado_com_typo_nao_arrasta_o_corpo_customizado() -> None:
    """Os espaços falham separadamente: o alerta é a coisa que existe para não
    ser perdida, e um typo no assunto não pode desfazer o corpo que estava certo."""
    settings = _settings(
        alertas_templates=json.dumps(
            {
                TipoAlerta.PENDENCIA_PARADA.value: {
                    "email_assunto": "Parada: {nao_existe}",
                    "email_corpo": "Corpo customizado de {operadora}",
                }
            }
        )
    )

    assunto, corpo = _email(
        TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings
    )

    assert assunto == "Pendência parada há 72h — Caberj"
    assert "nao_existe" not in assunto
    assert corpo == "Corpo customizado de Caberj"


def test_override_com_espaco_desconhecido_levanta_citando_os_validos() -> None:
    settings = _settings(
        alertas_templates=json.dumps({TipoAlerta.PENDENCIA_PARADA.value: {"e-mail_assunto": "x"}})
    )

    with pytest.raises(AlertConfigError) as erro:
        _email(TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings)

    mensagem = str(erro.value)
    assert "e-mail_assunto" in mensagem
    for espaco in ("whatsapp", "email_assunto", "email_corpo"):
        assert espaco in mensagem


# --- ALERTAS_CANAIS -------------------------------------------------------------


def test_canais_padrao_e_so_whatsapp() -> None:
    """Ligar o e-mail por padrão seria mandar mensagem que ninguém pediu."""
    assert config.canais_habilitados(_settings()) == {Canal.WHATSAPP}


def test_canais_aceita_lista_com_espaco_e_ordem_qualquer() -> None:
    assert config.canais_habilitados(_settings(alertas_canais=" email , whatsapp ")) == {
        Canal.WHATSAPP,
        Canal.EMAIL,
    }


def test_canais_vazio_desliga_tudo() -> None:
    assert config.canais_habilitados(_settings(alertas_canais="")) == set()


def test_canal_desconhecido_levanta_citando_os_validos() -> None:
    with pytest.raises(AlertConfigError) as erro:
        config.canais_habilitados(_settings(alertas_canais="whatsapp,telegrama"))

    mensagem = str(erro.value)
    assert "telegrama" in mensagem
    for canal in Canal:
        assert canal.value in mensagem


# --- ALERTAS_PAPEIS_EMAIL -------------------------------------------------------


def test_papeis_padrao_mandam_item_individual_so_ao_coordenador() -> None:
    """ASSUNÇÃO deste time, não requisito do cliente — ver `PAPEIS_EMAIL_PADRAO`."""
    resolvido = config.papeis_por_tipo(_settings())

    assert resolvido[TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO] == (Papel.COORDENADOR,)
    assert resolvido[TipoAlerta.DEADLINE_COMPETENCIA] == (Papel.COORDENADOR,)
    assert resolvido[TipoAlerta.PENDENCIA_PARADA] == (Papel.COORDENADOR,)


def test_o_gestor_so_recebe_o_unico_sinal_agregado_dos_quatro() -> None:
    """`volume_anormal` é leitura da operação; os outros três são item
    individual, e o gestor lê a operação sem executá-la (ADR 0001)."""
    resolvido = config.papeis_por_tipo(_settings())

    assert resolvido[TipoAlerta.VOLUME_ANORMAL] == (Papel.COORDENADOR, Papel.GESTOR)
    for tipo in TipoAlerta:
        if tipo is not TipoAlerta.VOLUME_ANORMAL:
            assert Papel.GESTOR not in resolvido[tipo]


def test_papeis_e_sobrescrita_parcial_e_nao_substituicao_do_mapa() -> None:
    """Exigir os quatro faria o esquecimento de um silenciar aquele alerta."""
    settings = _settings(
        alertas_papeis_email=json.dumps({TipoAlerta.VOLUME_ANORMAL.value: ["gestor"]})
    )

    resolvido = config.papeis_por_tipo(settings)

    assert resolvido[TipoAlerta.VOLUME_ANORMAL] == (Papel.GESTOR,)
    assert resolvido[TipoAlerta.PENDENCIA_PARADA] == (Papel.COORDENADOR,)


def test_papeis_com_lista_vazia_desliga_o_tipo_neste_canal() -> None:
    settings = _settings(alertas_papeis_email=json.dumps({TipoAlerta.VOLUME_ANORMAL.value: []}))

    assert config.papeis_por_tipo(settings)[TipoAlerta.VOLUME_ANORMAL] == ()


def test_papel_desconhecido_levanta_citando_os_validos() -> None:
    settings = _settings(
        alertas_papeis_email=json.dumps({TipoAlerta.VOLUME_ANORMAL.value: ["diretor"]})
    )

    with pytest.raises(AlertConfigError) as erro:
        config.papeis_por_tipo(settings)

    mensagem = str(erro.value)
    assert "diretor" in mensagem
    for papel in Papel:
        assert papel.value in mensagem
