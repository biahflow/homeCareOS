"""Vocabulário da classificação em buckets de glosa — issue #7.

Separado de `rules.schema` de propósito: o motor de regras responde "esta regra
foi satisfeita?", e a classificação responde "e daí, o que acontece com o
documento?". São perguntas de camadas diferentes, e só a segunda conhece
`DocumentoStatus`.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel

from homecareos.db.models.enums import DocumentoStatus


class TipoProblema(enum.StrEnum):
    """Natureza da pendência, derivada da `acao` da regra que reprovou.

    Gravado em `pendencias.tipo_problema`, que é `String` livre no banco — o
    enum existe aqui para a escrita ser fechada mesmo com a coluna aberta.
    """

    CAMPO_AUSENTE = "campo_ausente"  # regra com acao=rejeitar reprovou
    CAMPO_INVALIDO = "campo_invalido"  # regra com acao=sinalizar reprovou


class PendenciaProposta(BaseModel):
    """Pendência que a classificação decidiu abrir — ainda sem deadline nem responsável.

    Deadline e responsável dependem de I/O (a operadora do documento e a
    configuração da aplicação) e por isso são preenchidos pelo serviço, não
    pelo motor puro.
    """

    campo: str
    tipo_problema: TipoProblema
    descricao: str


class Classificacao(BaseModel):
    """Resultado puro da classificação: o bucket e as pendências a abrir."""

    status: DocumentoStatus
    """Só `APROVADO`, `PROBLEMA` ou `INCOMPLETO` — os três buckets de saída da
    avaliação de regras. Os demais membros de `DocumentoStatus` pertencem ao
    ciclo de correção, que é do serviço, não do motor."""

    pendencias: list[PendenciaProposta]
