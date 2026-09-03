"""CRUD de `regras` — issue #5.

Sem autenticação própria: a proteção é aplicada em `main.py`, no
`include_router(...)`, como para todos os outros — ver a docstring de
`api/auth.py` para por que a regra é por router e nunca endpoint a endpoint.
Desde a issue #30 a regra deste router é `exigir_papel(coordenador)`: a regra de
glosa é o que decide se um documento reprova, e editá-la é decisão de quem
coordena a conferência. A chave de API continua passando, como em todo o resto.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from homecareos.db.session import get_session
from homecareos.rules.errors import CondicaoInvalidaError, RegraNaoEncontradaError
from homecareos.rules.repository import (
    atualizar_regra,
    criar_regra,
    desativar_regra,
    listar_regras,
)
from homecareos.rules.schema import RegraCreate, RegraOut, RegraUpdate

router = APIRouter(prefix="/api", tags=["regras"])


@router.get("/regras", response_model=list[RegraOut])
def get_regras(
    session: Annotated[Session, Depends(get_session)],
    operadora_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[RegraOut]:
    return [RegraOut.de_regra(regra) for regra in listar_regras(session, operadora_id)]


@router.post("/regras", response_model=RegraOut, status_code=status.HTTP_201_CREATED)
def post_regra(body: RegraCreate, session: Annotated[Session, Depends(get_session)]) -> RegraOut:
    try:
        regra = criar_regra(
            session,
            operadora_id=body.operadora_id,
            campo=body.campo,
            condicao=body.condicao,
            acao=body.acao,
            motivo_glosa=body.motivo_glosa,
        )
    except CondicaoInvalidaError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return RegraOut.de_regra(regra)


@router.put("/regras/{regra_id}", response_model=RegraOut)
def put_regra(
    regra_id: uuid.UUID, body: RegraUpdate, session: Annotated[Session, Depends(get_session)]
) -> RegraOut:
    try:
        regra = atualizar_regra(
            session,
            regra_id,
            operadora_id=body.operadora_id,
            campo=body.campo,
            condicao=body.condicao,
            acao=body.acao,
            motivo_glosa=body.motivo_glosa,
        )
    except RegraNaoEncontradaError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CondicaoInvalidaError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return RegraOut.de_regra(regra)


@router.delete("/regras/{regra_id}", response_model=RegraOut)
def delete_regra(
    regra_id: uuid.UUID, session: Annotated[Session, Depends(get_session)]
) -> RegraOut:
    try:
        regra = desativar_regra(session, regra_id)
    except RegraNaoEncontradaError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return RegraOut.de_regra(regra)
