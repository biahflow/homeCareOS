"""Paginação por offset, usada por toda listagem da API.

Offset foi escolhido (em vez de cursor) porque as listagens desta trilha
(documentos, pendências, pacientes) são filtradas e ordenadas por colunas
simples, sem necessidade de estabilidade sob escrita concorrente pesada que
justificasse um cursor opaco. Envelope de resposta:

```json
{"data": [...], "paginacao": {"total": 120, "limite": 50, "offset": 0}}
```
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

LIMITE_PADRAO = 50
LIMITE_MAXIMO = 200


@dataclass(frozen=True)
class PaginacaoParams:
    limite: int
    offset: int


def paginacao_params(
    limite: Annotated[
        int, Query(ge=1, le=LIMITE_MAXIMO, description="Itens por página")
    ] = LIMITE_PADRAO,
    offset: Annotated[int, Query(ge=0, description="Itens a pular")] = 0,
) -> PaginacaoParams:
    return PaginacaoParams(limite=limite, offset=offset)


class Paginacao(BaseModel):
    total: int
    limite: int
    offset: int


class RespostaPaginada[T](BaseModel):
    data: list[T]
    paginacao: Paginacao


def envelope_paginado[T](
    *, itens: list[T], total: int, params: PaginacaoParams
) -> RespostaPaginada[T]:
    return RespostaPaginada[T](
        data=itens,
        paginacao=Paginacao(total=total, limite=params.limite, offset=params.offset),
    )
