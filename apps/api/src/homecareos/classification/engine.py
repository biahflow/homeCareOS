"""Motor de classificação em buckets de glosa — issue #7.

Puro: sem I/O, sem sessão de banco, sem relógio de parede na decisão do bucket.
Recebe os `ResultadoAvaliacao` que `rules.engine.validar()` produziu e responde
em que bucket o documento cai e que pendências precisam ser abertas. Quem
persiste isso é `classification/service.py`.

O bucket sai da `acao` da regra que reprovou — o campo que já existia em
`regras.acao` e que ninguém lia. Ver `_BUCKET_POR_ACAO`.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Sequence
from datetime import UTC, datetime

from homecareos.classification.schema import Classificacao, PendenciaProposta, TipoProblema
from homecareos.db.models.enums import DocumentoStatus, ResultadoValidacao
from homecareos.rules.schema import AcaoRegra, ResultadoAvaliacao

# Uma reprovação de regra `rejeitar` deixa o documento `incompleto`; uma de
# regra `sinalizar`, `problema`. `aprovar` não abre pendência nem afeta o
# bucket: é a ação de quem quer a validação registrada em `validacoes` sem
# segurar o documento.
_BUCKET_POR_ACAO: dict[AcaoRegra, tuple[DocumentoStatus, TipoProblema]] = {
    AcaoRegra.REJEITAR: (DocumentoStatus.INCOMPLETO, TipoProblema.CAMPO_AUSENTE),
    AcaoRegra.SINALIZAR: (DocumentoStatus.PROBLEMA, TipoProblema.CAMPO_INVALIDO),
}

_COMPETENCIA = re.compile(r"^(\d{4})-(\d{2})$")


def classificar(resultados: Sequence[ResultadoAvaliacao]) -> Classificacao:
    """Decide o bucket do documento e as pendências a abrir.

    `incompleto` tem precedência sobre `problema`: falta de campo obrigatório é
    mais grave que campo a conferir — o documento volta pro campo, e misturar
    os dois num bucket só perderia essa distinção justamente no caso pior.
    """
    pendencias: list[PendenciaProposta] = []
    incompleto = False
    problema = False

    for resultado in resultados:
        if resultado.resultado is not ResultadoValidacao.REPROVADO:
            continue
        bucket = _BUCKET_POR_ACAO.get(resultado.acao)
        if bucket is None:  # AcaoRegra.APROVAR: reprovou, mas não segura o documento
            continue
        status, tipo_problema = bucket
        incompleto = incompleto or status is DocumentoStatus.INCOMPLETO
        problema = problema or status is DocumentoStatus.PROBLEMA
        pendencias.append(
            PendenciaProposta(
                campo=resultado.campo,
                tipo_problema=tipo_problema,
                descricao=_descricao(resultado),
            )
        )

    if incompleto:
        status_final = DocumentoStatus.INCOMPLETO
    elif problema:
        status_final = DocumentoStatus.PROBLEMA
    else:
        status_final = DocumentoStatus.APROVADO

    return Classificacao(status=status_final, pendencias=pendencias)


def _descricao(resultado: ResultadoAvaliacao) -> str:
    """Junta o detalhe técnico da avaliação com o motivo de glosa da regra.

    `motivo_glosa` não é persistido em `validacoes` (a tabela não tem a coluna):
    a pendência é o único lugar em que ele sobrevive até o conferente, e é
    justamente o texto que ele precisa para saber o que a operadora glosaria.
    """
    descricao = f"{resultado.campo}: {resultado.detalhe}"
    if resultado.motivo_glosa is not None:
        descricao += f" (motivo de glosa: {resultado.motivo_glosa})"
    return descricao


def calcular_deadline(competencia: str, dia_envio: int) -> datetime:
    """Prazo da pendência: o `dia_envio` da operadora no mês seguinte à competência.

    O mês seguinte, e não o da própria competência, porque a competência só
    fecha quando o mês acaba — cobrar a correção dentro dele seria cobrar antes
    de o documento existir por inteiro.

    `dia_envio` maior que o último dia do mês alvo é clampado para o último dia
    (dia 31 em fevereiro vira 28, ou 29 em ano bissexto). Fim do dia em UTC:
    o prazo é "até o fim daquele dia", não "até a meia-noite que o inicia".
    """
    correspondencia = _COMPETENCIA.match(competencia)
    if correspondencia is None:
        raise ValueError(f"competência inválida: {competencia!r} (esperado 'YYYY-MM')")
    ano, mes = int(correspondencia.group(1)), int(correspondencia.group(2))
    if not 1 <= mes <= 12:
        raise ValueError(f"competência inválida: {competencia!r} (mês fora de 01..12)")

    ano_alvo, mes_alvo = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    ultimo_dia = calendar.monthrange(ano_alvo, mes_alvo)[1]
    return datetime(ano_alvo, mes_alvo, min(dia_envio, ultimo_dia), 23, 59, 59, tzinfo=UTC)
