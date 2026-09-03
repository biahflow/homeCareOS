"""Testes do motor de regras (issue #5). Puro: sem banco, sem I/O.

Espelha o estilo de `tests/test_extraction.py`: constrói `Regra` e
`EvolucaoProntuario` em memória (SQLAlchemy permite instanciar o model Python
puro sem sessão) e chama `engine.validar()` diretamente.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from homecareos.db.models.enums import ResultadoValidacao
from homecareos.db.models.regra import Regra
from homecareos.extraction.schema import CategoriaProfissional, EvolucaoProntuario
from homecareos.rules.engine import validar
from homecareos.rules.schema import CondicaoTypeAdapter


def _campos_completos(**overrides: object) -> EvolucaoProntuario:
    base = dict(
        nome_paciente="Maria da Silva",
        data_atendimento=date(2024, 3, 5),
        nome_profissional="João Souza",
        registro_coren="12.345",
        categoria_profissional=CategoriaProfissional.ENFERMEIRO,
        procedimentos_realizados=["curativo"],
        materiais_utilizados=["gaze"],
        assinatura_profissional_presente=True,
        carimbo_presente=True,
        carimbo_legivel=True,
        assinatura_paciente_responsavel_presente=True,
        observacoes=None,
        campos_ilegiveis=[],
        campos_incertos=[],
    )
    base.update(overrides)
    return EvolucaoProntuario(**base)  # type: ignore[arg-type]


def _regra(
    campo: str,
    condicao: dict[str, object],
    *,
    operadora_id: uuid.UUID | None = None,
    acao: str = "rejeitar",
    motivo_glosa: str = "Motivo de glosa padrão do teste",
) -> Regra:
    return Regra(
        id=uuid.uuid4(),
        operadora_id=operadora_id or uuid.uuid4(),
        campo=campo,
        condicao=json.dumps(condicao),
        acao=acao,
        motivo_glosa=motivo_glosa,
        ativo=True,
    )


# --- 1: Unimed — carimbo_legivel deve ser verdadeiro --------------------------


def test_unimed_carimbo_legivel_verdadeiro_aprova() -> None:
    regra = _regra("carimbo_legivel", {"tipo": "verdadeiro"})
    campos = _campos_completos(carimbo_legivel=True)

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.APROVADO
    assert resultado.motivo_glosa is None


def test_unimed_carimbo_legivel_falso_reprova() -> None:
    regra = _regra("carimbo_legivel", {"tipo": "verdadeiro"}, motivo_glosa="Carimbo ilegível")
    campos = _campos_completos(carimbo_legivel=False)

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.REPROVADO
    assert resultado.motivo_glosa == "Carimbo ilegível"


# --- 2: Amil — assinatura_profissional_presente deve ser verdadeira -----------


def test_amil_assinatura_profissional_presente_aprova() -> None:
    regra = _regra("assinatura_profissional_presente", {"tipo": "verdadeiro"})
    campos = _campos_completos(assinatura_profissional_presente=True)

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.APROVADO


def test_amil_assinatura_profissional_ausente_reprova() -> None:
    regra = _regra(
        "assinatura_profissional_presente",
        {"tipo": "verdadeiro"},
        motivo_glosa="Assinatura do profissional ausente",
    )
    campos = _campos_completos(assinatura_profissional_presente=False)

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.REPROVADO
    assert resultado.motivo_glosa == "Assinatura do profissional ausente"


# --- 3: data_atendimento dentro da competência faturada ------------------------


def test_data_atendimento_dentro_da_competencia_aprova() -> None:
    regra = _regra("data_atendimento", {"tipo": "dentro_da_competencia"})
    campos = _campos_completos(data_atendimento=date(2024, 3, 15))

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.APROVADO


def test_data_atendimento_fora_da_competencia_reprova() -> None:
    regra = _regra("data_atendimento", {"tipo": "dentro_da_competencia"})
    campos = _campos_completos(data_atendimento=date(2024, 4, 1))

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.REPROVADO


def test_data_atendimento_ausente_reprova() -> None:
    regra = _regra("data_atendimento", {"tipo": "dentro_da_competencia"})
    campos = _campos_completos(data_atendimento=None)

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.REPROVADO


# --- 4: registro_coren no formato NN.NNN ---------------------------------------


def test_registro_coren_formato_valido_aprova() -> None:
    regra = _regra("registro_coren", {"tipo": "formato", "regex": r"^\d{2}\.\d{3}$"})
    campos = _campos_completos(registro_coren="12.345")

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.APROVADO


def test_registro_coren_formato_invalido_reprova() -> None:
    regra = _regra("registro_coren", {"tipo": "formato", "regex": r"^\d{2}\.\d{3}$"})
    campos = _campos_completos(registro_coren="123.45")

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.REPROVADO


def test_registro_coren_ausente_reprova() -> None:
    regra = _regra("registro_coren", {"tipo": "formato", "regex": r"^\d{2}\.\d{3}$"})
    campos = _campos_completos(registro_coren=None)

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.REPROVADO


# --- 5: condicional se/quando/entao — glosa técnica ----------------------------


def _regra_condicional_tecnico() -> Regra:
    return _regra(
        "carimbo_legivel",
        {
            "tipo": "se",
            "quando": {
                "tipo": "formato",
                "regex": "^tecnico_enfermagem$",
                "campo": "categoria_profissional",
            },
            "entao": {"tipo": "verdadeiro"},
        },
        motivo_glosa="Carimbo ilegível para técnico de enfermagem",
    )


def test_condicional_tecnico_com_carimbo_ilegivel_reprova() -> None:
    regra = _regra_condicional_tecnico()
    campos = _campos_completos(
        categoria_profissional=CategoriaProfissional.TECNICO_ENFERMAGEM,
        carimbo_legivel=False,
    )

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.REPROVADO


def test_condicional_tecnico_com_carimbo_legivel_aprova() -> None:
    regra = _regra_condicional_tecnico()
    campos = _campos_completos(
        categoria_profissional=CategoriaProfissional.TECNICO_ENFERMAGEM,
        carimbo_legivel=True,
    )

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.APROVADO


def test_condicional_categoria_nao_tecnica_aprova_vacuamente() -> None:
    """A regra não se aplica: 'quando' é falso, então passa vacuamente,
    mesmo com carimbo ilegível."""
    regra = _regra_condicional_tecnico()
    campos = _campos_completos(
        categoria_profissional=CategoriaProfissional.ENFERMEIRO,
        carimbo_legivel=False,
    )

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.APROVADO


# --- 6: validar() nunca para na primeira reprovação -----------------------------


def test_validar_nao_para_na_primeira_reprovacao() -> None:
    regra_reprova = _regra("carimbo_legivel", {"tipo": "verdadeiro"})
    regra_aprova_1 = _regra("assinatura_profissional_presente", {"tipo": "verdadeiro"})
    regra_aprova_2 = _regra("carimbo_presente", {"tipo": "verdadeiro"})
    campos = _campos_completos(carimbo_legivel=False)

    resultados = validar(
        campos, [regra_reprova, regra_aprova_1, regra_aprova_2], competencia="2024-03"
    )

    assert len(resultados) == 3
    assert resultados[0].resultado is ResultadoValidacao.REPROVADO
    assert resultados[1].resultado is ResultadoValidacao.APROVADO
    assert resultados[2].resultado is ResultadoValidacao.APROVADO


# --- 7: campo inexistente no schema — reprova sem exceção -----------------------


def test_campo_inexistente_reprova_com_detalhe_sem_excecao() -> None:
    regra = _regra("campo_que_nao_existe", {"tipo": "presente"})
    campos = _campos_completos()

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.REPROVADO
    assert "campo_que_nao_existe" in resultado.detalhe


# --- 8: campo em campos_ilegiveis — reprova mesmo com valor bruto "bom" ---------


def test_campo_ilegivel_reprova_mesmo_com_valor_bruto_verdadeiro() -> None:
    regra = _regra("carimbo_legivel", {"tipo": "verdadeiro"})
    campos = _campos_completos(carimbo_legivel=True, campos_ilegiveis=["carimbo_legivel"])

    (resultado,) = validar(campos, [regra], competencia="2024-03")

    assert resultado.resultado is ResultadoValidacao.REPROVADO
    assert "ilegível" in resultado.detalhe


# --- 9: performance — ~50 regras variadas em menos de 1s ------------------------


def test_performance_50_regras_abaixo_de_1_segundo() -> None:
    regras: list[Regra] = []
    for i in range(50):
        tipo = i % 4
        if tipo == 0:
            regras.append(_regra("carimbo_legivel", {"tipo": "verdadeiro"}))
        elif tipo == 1:
            regras.append(_regra("registro_coren", {"tipo": "formato", "regex": r"^\d{2}\.\d{3}$"}))
        elif tipo == 2:
            regras.append(_regra("data_atendimento", {"tipo": "dentro_da_competencia"}))
        else:
            regras.append(_regra_condicional_tecnico())

    campos = _campos_completos()

    inicio = time.perf_counter()
    resultados = validar(campos, regras, competencia="2024-03")
    fim = time.perf_counter()

    assert len(resultados) == 50
    assert (fim - inicio) < 1.0


@pytest.mark.parametrize(
    "regex_patologica",
    ["^(a+)+$", "(x*)*", "^(ab+)+c$", "([0-9]+)*"],
)
def test_regex_com_quantificador_aninhado_e_recusada_na_escrita(regex_patologica: str) -> None:
    """`^(a+)+$` contra 28 caracteres leva mais de 5s — sozinho estoura o
    limite de 1s da issue e trava a conferência de uma competência inteira.

    A recusa é na escrita, não na avaliação: regra patológica não pode entrar
    no banco e explodir depois, no meio de um fechamento.
    """
    with pytest.raises(ValidationError):
        CondicaoTypeAdapter.validate_python({"tipo": "formato", "regex": regex_patologica})


def test_regex_legitima_de_coren_continua_aceita() -> None:
    """A guarda não pode barrar o caso real da issue: formato de COREN."""
    condicao = CondicaoTypeAdapter.validate_python({"tipo": "formato", "regex": r"^\d{2}\.\d{3}$"})

    assert condicao.tipo == "formato"
