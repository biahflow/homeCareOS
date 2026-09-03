"""Testes unitários dos templates e da configuração de alertas (issue #9).

Sem banco e sem rede: tudo aqui é função pura sobre `Settings` construída à mão.
"""

from __future__ import annotations

import json

import pytest

from homecareos.alerts import config, templates
from homecareos.alerts.errors import AlertConfigError
from homecareos.alerts.schema import TipoAlerta
from homecareos.config import Settings

CONTEXTOS: dict[TipoAlerta, dict[str, str]] = {
    TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO: {
        "paciente": "Maria de Souza",
        "operadora": "Unimed",
        "problema": "carimbo ausente",
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
        "operadora": "Caberj",
        "problema": "assinatura ausente",
        "horas": "72",
        "deadline": "10/04/2099",
    },
}


def _settings(**overrides: str) -> Settings:
    return Settings(**overrides)


# --- templates ----------------------------------------------------------------


@pytest.mark.parametrize("tipo", list(TipoAlerta))
def test_template_padrao_renderiza_com_contexto_completo(tipo: TipoAlerta) -> None:
    contexto = CONTEXTOS[tipo]

    mensagem = templates.renderizar(tipo, contexto, _settings())

    for valor in contexto.values():
        assert valor in mensagem
    assert "{" not in mensagem


def test_override_valido_substitui_o_template_padrao() -> None:
    settings = _settings(
        alertas_templates=json.dumps(
            {TipoAlerta.PENDENCIA_PARADA.value: "Parada há {horas}h: {problema}"}
        )
    )

    mensagem = templates.renderizar(
        TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings
    )

    assert mensagem == "Parada há 72h: assinatura ausente"


def test_override_com_placeholder_inexistente_cai_para_o_padrao_sem_levantar() -> None:
    """Template customizado com typo não pode calar o alerta."""
    settings = _settings(
        alertas_templates=json.dumps({TipoAlerta.PENDENCIA_PARADA.value: "Parada: {nao_existe}"})
    )

    mensagem = templates.renderizar(
        TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings
    )

    assert mensagem == templates.TEMPLATES_PADRAO[TipoAlerta.PENDENCIA_PARADA].format_map(
        CONTEXTOS[TipoAlerta.PENDENCIA_PARADA]
    )
    assert "nao_existe" not in mensagem


def test_valor_ausente_no_contexto_nunca_vira_none_na_mensagem() -> None:
    contexto = dict(CONTEXTOS[TipoAlerta.PENDENCIA_PARADA])
    del contexto["paciente"]

    mensagem = templates.renderizar(TipoAlerta.PENDENCIA_PARADA, contexto, _settings())

    assert f"Paciente: {templates.VALOR_AUSENTE}" in mensagem
    assert "None" not in mensagem


def test_templates_com_json_malformado_levanta_alert_config_error() -> None:
    settings = _settings(alertas_templates="{não é json}")

    with pytest.raises(AlertConfigError) as erro:
        templates.renderizar(
            TipoAlerta.PENDENCIA_PARADA, CONTEXTOS[TipoAlerta.PENDENCIA_PARADA], settings
        )

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
