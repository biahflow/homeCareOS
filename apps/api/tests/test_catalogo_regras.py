"""Testes unitários do catálogo de regras (issue #10) — sem banco.

Cobre os critérios de aceite de auditoria da issue: mínimo de regras por
operadora, separação genérica/específica por `escopo`, fonte documentada em
toda regra (com o marcador `A CONFIRMAR` no que não foi verificado), e o fato
de o catálogo inteiro ser avaliável pelo motor de regras.
"""

from __future__ import annotations

import uuid

from homecareos.db.models.regra import Regra
from homecareos.extraction.schema import EvolucaoProntuario
from homecareos.rules.catalogo import carregar_por_operadora, carregar_tiss
from homecareos.rules.engine import validar
from homecareos.rules.schema import AcaoRegra, CondicaoTypeAdapter, EscopoRegra


def test_carregar_tiss_devolve_treze_regras_ativas_de_escopo_tiss() -> None:
    catalogo = carregar_tiss()

    assert len(catalogo) == 13
    assert all(regra.escopo is EscopoRegra.TISS for regra in catalogo)
    assert all(regra.ativo is True for regra in catalogo)


def test_carregar_por_operadora_tem_amil_e_unimed_inativas() -> None:
    por_operadora = carregar_por_operadora()

    assert set(por_operadora.keys()) == {"AMIL", "UNIMED"}
    for catalogo in por_operadora.values():
        assert len(catalogo) == 6
        assert all(regra.escopo is EscopoRegra.OPERADORA for regra in catalogo)
        assert all(regra.ativo is False for regra in catalogo)


def test_toda_regra_especifica_tem_fonte_a_confirmar() -> None:
    """Invariante de honestidade: específica não verificada nunca nasce parecendo verificada."""
    for catalogo in carregar_por_operadora().values():
        for regra in catalogo:
            assert regra.fonte.startswith("A CONFIRMAR")


def test_toda_regra_de_todo_catalogo_tem_fonte_e_campo_valido() -> None:
    catalogos = [carregar_tiss(), *carregar_por_operadora().values()]
    for catalogo in catalogos:
        for regra in catalogo:
            assert regra.fonte.strip() != ""
            assert regra.campo in EvolucaoProntuario.model_fields


def test_codigo_e_unico_por_catalogo_e_nao_colide_entre_tiss_e_operadora() -> None:
    codigos_tiss = [regra.codigo for regra in carregar_tiss()]
    assert len(codigos_tiss) == len(set(codigos_tiss))

    codigos_operadora: set[str] = set()
    for catalogo in carregar_por_operadora().values():
        codigos_do_catalogo = [regra.codigo for regra in catalogo]
        assert len(codigos_do_catalogo) == len(set(codigos_do_catalogo))
        codigos_operadora.update(codigos_do_catalogo)

    assert set(codigos_tiss).isdisjoint(codigos_operadora)


def test_toda_condicao_e_aceita_pelo_type_adapter() -> None:
    """Prova que o guard de quantificador aninhado não recusa nenhum padrão do catálogo."""
    catalogos = [carregar_tiss(), *carregar_por_operadora().values()]
    for catalogo in catalogos:
        for regra in catalogo:
            CondicaoTypeAdapter.validate_python(regra.condicao)


def test_cada_operadora_com_catalogo_proprio_tem_pelo_menos_dezenove_regras() -> None:
    """Critério de aceite da issue #10: >= 10 regras por operadora (13 + 6 = 19)."""
    total_tiss = len(carregar_tiss())
    for catalogo in carregar_por_operadora().values():
        assert total_tiss + len(catalogo) == 19
        assert total_tiss + len(catalogo) >= 10


def test_catalogo_tiss_e_avaliavel_pelo_motor_e_reprova_evolucao_vazia() -> None:
    """Sanidade de ponta a ponta, ainda sem banco: o catálogo casa com o schema de extração."""
    regras = [
        Regra(
            id=uuid.uuid4(),
            operadora_id=uuid.uuid4(),
            campo=regra_catalogo.campo,
            condicao=CondicaoTypeAdapter.validate_python(regra_catalogo.condicao).model_dump_json(),
            acao=regra_catalogo.acao.value,
            motivo_glosa=regra_catalogo.motivo_glosa,
            ativo=regra_catalogo.ativo,
            codigo=regra_catalogo.codigo,
            fonte=regra_catalogo.fonte,
            escopo=regra_catalogo.escopo.value,
        )
        for regra_catalogo in carregar_tiss()
    ]

    resultados = validar(EvolucaoProntuario(), regras, competencia="2099-03")

    resultados_por_regra_id = {resultado.regra_id: resultado for resultado in resultados}
    regras_rejeitar = [regra for regra in regras if AcaoRegra(regra.acao) is AcaoRegra.REJEITAR]
    assert regras_rejeitar, "catálogo precisa ter ao menos uma regra 'rejeitar' para o teste valer"
    for regra in regras_rejeitar:
        resultado = resultados_por_regra_id[regra.id]
        assert resultado.resultado.value == "reprovado", (
            f"regra {regra.codigo!r} deveria reprovar contra evolução vazia: {resultado.detalhe}"
        )
