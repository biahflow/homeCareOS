"""Motor de avaliação de regras de operadora — issue #5.

Puro: sem I/O, sem sessão de banco, sem rede. Recebe as regras já carregadas
(quem decide quais passar — normalmente só as `ativo=True` de uma operadora —
é `rules.repository.buscar_regras_ativas` ou o dispatcher que encadeia a
extração) e avalia todas, sem parar na primeira reprovação: o conferente
precisa ver a lista inteira de violações de um documento, não só a primeira.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from typing import Any

from pydantic import ValidationError

from homecareos.db.models.enums import ResultadoValidacao
from homecareos.db.models.regra import Regra
from homecareos.extraction.schema import EvolucaoProntuario
from homecareos.rules.schema import (
    Condicao,
    CondicaoDentroDaCompetencia,
    CondicaoE,
    CondicaoFormato,
    CondicaoOu,
    CondicaoPresente,
    CondicaoSe,
    CondicaoTypeAdapter,
    CondicaoVerdadeiro,
    ResultadoAvaliacao,
)


def validar(
    campos: EvolucaoProntuario,
    regras: Sequence[Regra],
    *,
    competencia: str,
) -> list[ResultadoAvaliacao]:
    """Avalia `campos` contra cada regra em `regras`; nunca para na primeira reprovação."""
    return [_avaliar_regra(campos, regra, competencia=competencia) for regra in regras]


def _avaliar_regra(
    campos: EvolucaoProntuario, regra: Regra, *, competencia: str
) -> ResultadoAvaliacao:
    campo = regra.campo
    try:
        condicao = CondicaoTypeAdapter.validate_json(regra.condicao)
    except ValidationError as exc:
        # Defensivo: a escrita (rules.repository) já valida a condição antes de
        # gravar; se mesmo assim uma condição malformada chegar aqui (dado
        # legado, edição manual no banco), reprova em vez de estourar no meio
        # de um fechamento de competência.
        return ResultadoAvaliacao(
            campo=campo,
            regra_id=regra.id,
            resultado=ResultadoValidacao.REPROVADO,
            detalhe=f"Condição da regra malformada: {exc}",
            motivo_glosa=regra.motivo_glosa,
        )

    satisfeita, detalhe_falha = _avaliar_condicao(condicao, campos, campo, competencia)
    if satisfeita:
        return ResultadoAvaliacao(
            campo=campo,
            regra_id=regra.id,
            resultado=ResultadoValidacao.APROVADO,
            detalhe="Regra satisfeita.",
            motivo_glosa=None,
        )
    return ResultadoAvaliacao(
        campo=campo,
        regra_id=regra.id,
        resultado=ResultadoValidacao.REPROVADO,
        detalhe=detalhe_falha,
        motivo_glosa=regra.motivo_glosa,
    )


def _avaliar_condicao(
    condicao: Condicao,
    campos: EvolucaoProntuario,
    campo_padrao: str,
    competencia: str,
) -> tuple[bool, str]:
    if isinstance(condicao, CondicaoE):
        for clausula in condicao.clausulas:
            ok, detalhe = _avaliar_condicao(clausula, campos, campo_padrao, competencia)
            if not ok:
                return False, detalhe
        return True, ""

    if isinstance(condicao, CondicaoOu):
        detalhes: list[str] = []
        for clausula in condicao.clausulas:
            ok, detalhe = _avaliar_condicao(clausula, campos, campo_padrao, competencia)
            if ok:
                return True, ""
            detalhes.append(detalhe)
        return False, " ou ".join(detalhes) if detalhes else "Nenhuma cláusula de 'ou' satisfeita."

    if isinstance(condicao, CondicaoSe):
        quando_ok, _ = _avaliar_condicao(condicao.quando, campos, campo_padrao, competencia)
        if not quando_ok:
            return True, ""  # condição não se aplica a este documento: passa vacuamente
        return _avaliar_condicao(condicao.entao, campos, campo_padrao, competencia)

    # A partir daqui só sobram as condições-folha: Presente/Verdadeiro/Formato/DentroDaCompetencia.
    campo_alvo = condicao.campo or campo_padrao
    if campo_alvo not in EvolucaoProntuario.model_fields:
        return False, f"Campo '{campo_alvo}' não existe no schema de extração."
    if campo_alvo in campos.campos_ilegiveis:
        return False, f"Campo '{campo_alvo}' foi marcado como ilegível pela extração."
    valor: Any = getattr(campos, campo_alvo)

    if isinstance(condicao, CondicaoPresente):
        ok = valor is not None and (not isinstance(valor, list) or len(valor) > 0)
        return ok, "" if ok else f"Campo '{campo_alvo}' está ausente."

    if isinstance(condicao, CondicaoVerdadeiro):
        ok = valor is True
        return ok, "" if ok else f"Campo '{campo_alvo}' não é verdadeiro."

    if isinstance(condicao, CondicaoFormato):
        if valor is None:
            return False, f"Campo '{campo_alvo}' está ausente para checagem de formato."
        ok = re.fullmatch(condicao.regex, str(valor)) is not None
        detalhe = (
            ""
            if ok
            else f"Campo '{campo_alvo}' não bate com o formato esperado ({condicao.regex})."
        )
        return ok, detalhe

    if isinstance(condicao, CondicaoDentroDaCompetencia):
        if not isinstance(valor, date):
            return False, f"Campo '{campo_alvo}' está ausente ou não é uma data."
        competencia_do_valor = f"{valor.year:04d}-{valor.month:02d}"
        ok = competencia_do_valor == competencia
        detalhe = (
            ""
            if ok
            else (
                f"Data do campo '{campo_alvo}' ({competencia_do_valor}) fora da "
                f"competência faturada ({competencia})."
            )
        )
        return ok, detalhe

    raise AssertionError(f"tipo de condição não tratado: {condicao!r}")  # inalcançável
