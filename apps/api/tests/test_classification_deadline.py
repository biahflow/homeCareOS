"""Testes de `calcular_deadline` (issue #7). Puro: sem banco, sem relógio.

O prazo da pendência é o único número desta trilha que uma pessoa de operação
vai conferir no calendário. Os casos de borda aqui (fevereiro, mês de 30 dias,
virada de ano) são os que produziriam uma data inexistente — `datetime` levanta
`ValueError` em `31 de fevereiro`, e isso derrubaria o upload.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from homecareos.classification.engine import calcular_deadline


def test_deadline_cai_no_mes_seguinte_a_competencia() -> None:
    """A competência só fecha quando o mês acaba — o prazo é do mês seguinte."""
    assert calcular_deadline("2024-03", 10) == datetime(2024, 4, 10, 23, 59, 59, tzinfo=UTC)


def test_deadline_e_o_fim_do_dia_em_utc() -> None:
    """`23:59:59` e não `00:00`: o prazo é 'até o fim daquele dia'."""
    deadline = calcular_deadline("2024-03", 10)

    assert (deadline.hour, deadline.minute, deadline.second) == (23, 59, 59)
    assert deadline.tzinfo is UTC


def test_dezembro_vira_janeiro_do_ano_seguinte() -> None:
    assert calcular_deadline("2024-12", 5) == datetime(2025, 1, 5, 23, 59, 59, tzinfo=UTC)


# --- clamp: dia de envio maior que o último dia do mês alvo -------------------


def test_dia_31_em_fevereiro_comum_e_clampado_para_28() -> None:
    assert calcular_deadline("2025-01", 31) == datetime(2025, 2, 28, 23, 59, 59, tzinfo=UTC)


def test_dia_31_em_fevereiro_bissexto_e_clampado_para_29() -> None:
    assert calcular_deadline("2024-01", 31) == datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC)


def test_dia_31_em_mes_de_30_dias_e_clampado_para_30() -> None:
    """Competência de março cai em abril, que tem 30 dias."""
    assert calcular_deadline("2024-03", 31) == datetime(2024, 4, 30, 23, 59, 59, tzinfo=UTC)


def test_dia_dentro_do_mes_nao_e_clampado() -> None:
    assert calcular_deadline("2024-01", 28) == datetime(2024, 2, 28, 23, 59, 59, tzinfo=UTC)


# --- competência malformada ---------------------------------------------------


@pytest.mark.parametrize(
    "competencia",
    ["", "2024", "2024-3", "24-03", "2024/03", "2024-03-15", "março/2024", "abcd-ef"],
)
def test_competencia_malformada_levanta_value_error(competencia: str) -> None:
    """`documentos.competencia` é `String` livre — o motor recusa, o serviço trata."""
    with pytest.raises(ValueError):
        calcular_deadline(competencia, 10)


def test_mes_fora_do_intervalo_levanta_value_error() -> None:
    """Passa no formato `YYYY-MM` mas não é um mês: `13` não pode virar data."""
    with pytest.raises(ValueError):
        calcular_deadline("2024-13", 10)
