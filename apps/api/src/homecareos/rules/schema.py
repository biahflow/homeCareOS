"""Schema declarativo da condição de uma `Regra` e do resultado de sua avaliação.

A gramática (`Condicao`) é o que garante o critério de aceite central da issue
#5: adicionar uma regra nova é gravar um JSON novo em `regras.condicao`, nunca
escrever `if operadora == "..."` em código. Ver `rules/engine.py` para como ela
é avaliada.
"""

from __future__ import annotations

import enum
import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from homecareos.db.models.enums import ResultadoValidacao
from homecareos.db.models.regra import Regra


class CondicaoBase(BaseModel):
    campo: str | None = None


class CondicaoPresente(CondicaoBase):
    tipo: Literal["presente"]


class CondicaoVerdadeiro(CondicaoBase):
    tipo: Literal["verdadeiro"]


class CondicaoFormato(CondicaoBase):
    tipo: Literal["formato"]
    regex: str

    @field_validator("regex")
    @classmethod
    def _regex_compilavel(cls, valor: str) -> str:
        try:
            re.compile(valor)
        except re.error as exc:
            raise ValueError(f"regex inválida: {exc}") from exc
        _recusar_quantificador_aninhado(valor)
        return valor


# Quantificador aplicado a um grupo que já contém quantificador — `(a+)+`,
# `(a*)*`, `(x+y*)+`. É o construto que produz backtracking catastrófico: um
# alvo de 28 caracteres contra `^(a+)+$` leva mais de 5 segundos, o que sozinho
# estoura o limite de 1s da issue e trava a conferência de uma competência.
_QUANTIFICADOR_ANINHADO = re.compile(r"\([^()]*[*+][^()]*\)\s*[*+{]")


def _recusar_quantificador_aninhado(valor: str) -> None:
    """Recusa, na escrita, o padrão mais comum de backtracking catastrófico.

    Heurística, **não** garantia: existem outras formas de escrever um regex
    patológico. A defesa completa seria avaliar com timeout (ver
    `rules/engine.py`). Isto barra o caso acidental — alguém de operação
    colando um padrão sem perceber — que é o cenário que a issue #5 torna
    provável ao permitir regra nova sem deploy.
    """
    if _QUANTIFICADOR_ANINHADO.search(valor):
        raise ValueError(
            "regex com quantificador aninhado (ex.: `(a+)+`) é recusada: esse "
            "construto pode levar segundos ou minutos para avaliar. Reescreva "
            "sem repetição dentro de grupo repetido."
        )


class CondicaoDentroDaCompetencia(CondicaoBase):
    tipo: Literal["dentro_da_competencia"]


class CondicaoE(BaseModel):
    tipo: Literal["e"]
    clausulas: list[Condicao] = Field(min_length=1)


class CondicaoOu(BaseModel):
    tipo: Literal["ou"]
    clausulas: list[Condicao] = Field(min_length=1)


class CondicaoSe(BaseModel):
    tipo: Literal["se"]
    quando: Condicao
    entao: Condicao


CondicaoUniao = (
    CondicaoPresente
    | CondicaoVerdadeiro
    | CondicaoFormato
    | CondicaoDentroDaCompetencia
    | CondicaoE
    | CondicaoOu
    | CondicaoSe
)
Condicao = Annotated[CondicaoUniao, Field(discriminator="tipo")]

CondicaoE.model_rebuild()
CondicaoOu.model_rebuild()
CondicaoSe.model_rebuild()

CondicaoTypeAdapter: TypeAdapter[Condicao] = TypeAdapter(Condicao)


class EscopoRegra(enum.StrEnum):
    """De onde a regra vem, e por isso quanto ela vale como prova.

    `TISS` é regra genérica com fonte normativa pública (RDC Anvisa 11/2006,
    Resolução Cofen 754/2024, padrão TISS/ANS): vale para qualquer operadora e
    nasce ativa. `OPERADORA` é exigência de uma operadora específica, que só é
    defensável contra o manual do prestador dela.
    """

    TISS = "tiss"
    OPERADORA = "operadora"


class AcaoRegra(enum.StrEnum):
    """O que fazer quando a condição da regra NÃO é satisfeita.

    Existe como enum, e não como `Literal`, porque a classificação em buckets
    de glosa (`classification/engine.py`) precisa comparar contra membros e
    não contra string crua: é `acao` que decide se a reprovação vira
    `incompleto` (campo obrigatório faltando, volta pro campo) ou `problema`
    (algo a conferir antes do envio).
    """

    APROVAR = "aprovar"
    SINALIZAR = "sinalizar"
    REJEITAR = "rejeitar"


class ResultadoAvaliacao(BaseModel):
    """Resultado da avaliação de uma `Regra` contra um `EvolucaoProntuario`.

    Nome deliberadamente diferente de `ResultadoValidacao` (o enum
    aprovado/reprovado de `db.models.enums`, reusado aqui no campo
    `resultado`) para não colidir com ele.
    """

    campo: str
    regra_id: uuid.UUID
    resultado: ResultadoValidacao
    detalhe: str
    # Sem default de propósito: a `acao` da regra é o que determina o bucket do
    # documento, e um default silencioso esconderia o esquecimento de passá-la.
    acao: AcaoRegra
    motivo_glosa: str | None = None


class RegraCreate(BaseModel):
    operadora_id: uuid.UUID
    campo: str = Field(min_length=1)
    condicao: dict[str, Any]
    acao: AcaoRegra
    motivo_glosa: str = Field(min_length=1)


class RegraUpdate(RegraCreate):
    """Mesmo corpo do create — PUT substitui a regra inteira (não é PATCH parcial)."""


class RegraOut(BaseModel):
    id: uuid.UUID
    operadora_id: uuid.UUID
    campo: str
    condicao: dict[str, Any]
    acao: str
    motivo_glosa: str
    ativo: bool
    created_at: datetime
    # Aditivo (issue #10): não quebra o contrato atual. `None`/`"operadora"`
    # para regra criada via `POST /api/regras`, que não vem de catálogo.
    codigo: str | None
    fonte: str | None
    escopo: str

    @classmethod
    def de_regra(cls, regra: Regra) -> RegraOut:
        return cls(
            id=regra.id,
            operadora_id=regra.operadora_id,
            campo=regra.campo,
            condicao=json.loads(regra.condicao),
            acao=regra.acao,
            motivo_glosa=regra.motivo_glosa,
            ativo=regra.ativo,
            created_at=regra.created_at,
            codigo=regra.codigo,
            fonte=regra.fonte,
            escopo=regra.escopo,
        )
