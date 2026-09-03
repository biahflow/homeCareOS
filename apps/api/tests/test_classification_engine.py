"""Testes do motor de classificação em buckets (issue #7). Puro: sem banco, sem I/O.

Constrói `ResultadoAvaliacao` diretamente — é o contrato de entrada de
`classificar()`, e amarrá-lo aqui protege o motor de mudanças acidentais em
`rules.engine`, que é quem o produz de verdade.
"""

from __future__ import annotations

import json
import uuid

from homecareos.classification.engine import classificar
from homecareos.classification.schema import TipoProblema
from homecareos.db.models.enums import DocumentoStatus, ResultadoValidacao
from homecareos.db.models.regra import Regra
from homecareos.extraction.schema import EvolucaoProntuario
from homecareos.rules.engine import validar
from homecareos.rules.schema import AcaoRegra, ResultadoAvaliacao


def _resultado(
    *,
    campo: str = "carimbo_legivel",
    resultado: ResultadoValidacao = ResultadoValidacao.REPROVADO,
    acao: AcaoRegra = AcaoRegra.REJEITAR,
    detalhe: str = "Campo 'carimbo_legivel' não é verdadeiro.",
    motivo_glosa: str | None = None,
) -> ResultadoAvaliacao:
    return ResultadoAvaliacao(
        campo=campo,
        regra_id=uuid.uuid4(),
        resultado=resultado,
        detalhe=detalhe,
        acao=acao,
        motivo_glosa=motivo_glosa,
    )


# --- buckets e precedência ----------------------------------------------------


def test_sem_resultados_aprova_sem_pendencia() -> None:
    classificacao = classificar([])

    assert classificacao.status is DocumentoStatus.APROVADO
    assert classificacao.pendencias == []


def test_tudo_aprovado_aprova_sem_pendencia() -> None:
    resultados = [
        _resultado(resultado=ResultadoValidacao.APROVADO, acao=AcaoRegra.REJEITAR),
        _resultado(resultado=ResultadoValidacao.APROVADO, acao=AcaoRegra.SINALIZAR),
    ]

    classificacao = classificar(resultados)

    assert classificacao.status is DocumentoStatus.APROVADO
    assert classificacao.pendencias == []


def test_reprovacao_de_regra_sinalizar_vira_problema() -> None:
    classificacao = classificar([_resultado(acao=AcaoRegra.SINALIZAR)])

    assert classificacao.status is DocumentoStatus.PROBLEMA
    assert [p.tipo_problema for p in classificacao.pendencias] == [TipoProblema.CAMPO_INVALIDO]


def test_reprovacao_de_regra_rejeitar_vira_incompleto() -> None:
    classificacao = classificar([_resultado(acao=AcaoRegra.REJEITAR)])

    assert classificacao.status is DocumentoStatus.INCOMPLETO
    assert [p.tipo_problema for p in classificacao.pendencias] == [TipoProblema.CAMPO_AUSENTE]


def test_um_rejeitar_no_meio_de_sinalizar_manda_o_documento_pra_incompleto() -> None:
    """Precedência: falta de campo obrigatório é mais grave e ganha o bucket."""
    resultados = [
        _resultado(campo="observacoes", acao=AcaoRegra.SINALIZAR),
        _resultado(campo="registro_coren", acao=AcaoRegra.REJEITAR),
        _resultado(campo="carimbo_legivel", acao=AcaoRegra.SINALIZAR),
    ]

    classificacao = classificar(resultados)

    assert classificacao.status is DocumentoStatus.INCOMPLETO
    # Todas as reprovações viram pendência, não só as do bucket vencedor.
    assert len(classificacao.pendencias) == 3


def test_acao_aprovar_que_reprova_nao_gera_pendencia_nem_muda_bucket() -> None:
    """`acao=aprovar` registra a violação em `validacoes` e não segura o documento."""
    classificacao = classificar([_resultado(acao=AcaoRegra.APROVAR)])

    assert classificacao.status is DocumentoStatus.APROVADO
    assert classificacao.pendencias == []


def test_acao_aprovar_nao_contamina_o_bucket_das_outras() -> None:
    resultados = [
        _resultado(campo="observacoes", acao=AcaoRegra.APROVAR),
        _resultado(campo="carimbo_legivel", acao=AcaoRegra.SINALIZAR),
    ]

    classificacao = classificar(resultados)

    assert classificacao.status is DocumentoStatus.PROBLEMA
    assert [p.campo for p in classificacao.pendencias] == ["carimbo_legivel"]


def test_acao_desconhecida_no_banco_cai_em_sinalizar() -> None:
    """`regras.acao` é `String` livre: valor desconhecido tem que sinalizar, não sumir.

    Vai de ponta a ponta (`validar()` -> `classificar()`) porque é justamente na
    junta entre os dois que o valor cru do banco é traduzido: o documento
    precisa virar `problema` e ser olhado por um humano, nunca ser aprovado por
    causa de um `acao` digitado errado.
    """
    regra = Regra(
        id=uuid.uuid4(),
        operadora_id=uuid.uuid4(),
        campo="carimbo_legivel",
        condicao=json.dumps({"tipo": "verdadeiro"}),
        acao="glosar",  # valor que não existe em AcaoRegra
        motivo_glosa="Carimbo ilegível",
        ativo=True,
    )
    campos = EvolucaoProntuario(carimbo_legivel=False)

    classificacao = classificar(validar(campos, [regra], competencia="2024-03"))

    assert classificacao.status is DocumentoStatus.PROBLEMA
    assert [p.tipo_problema for p in classificacao.pendencias] == [TipoProblema.CAMPO_INVALIDO]


# --- forma das pendências -----------------------------------------------------


def test_ordem_das_pendencias_segue_a_ordem_dos_resultados() -> None:
    """Determinismo: a ordem das regras é a ordem que o conferente vê na tela."""
    campos = ["registro_coren", "carimbo_legivel", "data_atendimento", "nome_paciente"]
    resultados = [_resultado(campo=campo, acao=AcaoRegra.SINALIZAR) for campo in campos]

    classificacao = classificar(resultados)

    assert [p.campo for p in classificacao.pendencias] == campos


def test_descricao_junta_campo_e_detalhe() -> None:
    resultado = _resultado(
        campo="registro_coren",
        detalhe="Campo 'registro_coren' está ausente.",
        motivo_glosa=None,
    )

    (pendencia,) = classificar([resultado]).pendencias

    assert pendencia.descricao == "registro_coren: Campo 'registro_coren' está ausente."


def test_descricao_carrega_o_motivo_de_glosa_da_regra() -> None:
    """`motivo_glosa` não é persistido em `validacoes` — a pendência é onde ele sobrevive."""
    resultado = _resultado(
        campo="carimbo_legivel",
        detalhe="Campo 'carimbo_legivel' não é verdadeiro.",
        motivo_glosa="Carimbo ilegível",
    )

    (pendencia,) = classificar([resultado]).pendencias

    assert pendencia.descricao == (
        "carimbo_legivel: Campo 'carimbo_legivel' não é verdadeiro. "
        "(motivo de glosa: Carimbo ilegível)"
    )
