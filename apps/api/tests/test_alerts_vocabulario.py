"""Testes do vocabulário de campo -> rótulo humano (issue #9).

Sem banco: função pura sobre strings.
"""

from __future__ import annotations

import pytest

from homecareos.alerts.vocabulario import CAMPO_NAO_IDENTIFICADO, ROTULOS_DE_CAMPO, rotulo_de_campo

# Os onze campos que `rules/data/*.json` usa.
CAMPOS_CONHECIDOS = [
    "assinatura_paciente_responsavel_presente",
    "assinatura_profissional_presente",
    "carimbo_legivel",
    "carimbo_presente",
    "categoria_profissional",
    "data_atendimento",
    "materiais_utilizados",
    "nome_paciente",
    "nome_profissional",
    "procedimentos_realizados",
    "registro_coren",
]


def test_vocabulario_cobre_exatamente_os_onze_campos_conhecidos() -> None:
    assert set(ROTULOS_DE_CAMPO) == set(CAMPOS_CONHECIDOS)


@pytest.mark.parametrize("campo", CAMPOS_CONHECIDOS)
def test_rotulo_de_campo_conhecido_nao_repete_o_nome_tecnico(campo: str) -> None:
    """O defeito que a primeira mensagem real expôs: o nome técnico do campo não pode aparecer
    duplicado (uma vez como rótulo, outra como nome de coluna)."""
    rotulo = rotulo_de_campo(campo)

    assert rotulo != campo
    assert campo not in rotulo
    assert "_" not in rotulo


def test_rotulo_de_campo_fora_do_vocabulario_cai_no_proprio_nome() -> None:
    """O caso que garante que o alerta não desaparece: uma regra nova, criada
    pela API depois desta entrega, ainda não tem rótulo bonito, mas o campo
    continua indo para a mensagem."""
    assert rotulo_de_campo("campo_recem_criado_pela_api") == "campo_recem_criado_pela_api"


def test_rotulo_de_campo_none_cai_no_fallback_previsivel() -> None:
    """Pendência anterior à issue #7 (`Pendencia.campo IS NULL`) não pode
    quebrar a formatação nem silenciar o alerta."""
    assert rotulo_de_campo(None) == CAMPO_NAO_IDENTIFICADO


def test_rotulo_de_campo_string_vazia_cai_no_mesmo_fallback_de_none() -> None:
    assert rotulo_de_campo("") == CAMPO_NAO_IDENTIFICADO
