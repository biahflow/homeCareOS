"""`GET /api/pacientes` (paginado, filtro de operadora) e `POST /api/pacientes`."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from homecareos.api.pagination import (
    PaginacaoParams,
    RespostaPaginada,
    envelope_paginado,
    paginacao_params,
)
from homecareos.db.models import Modalidade, Operadora, Paciente
from homecareos.db.session import get_session

router = APIRouter(prefix="/api/pacientes", tags=["pacientes"])


class PacienteItem(BaseModel):
    id: uuid.UUID
    nome: str
    operadora_id: uuid.UUID
    modalidade: Modalidade
    data_vencimento_pad: date | None
    status_pad: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PacienteCriar(BaseModel):
    nome: str
    operadora_id: uuid.UUID
    modalidade: Modalidade
    data_vencimento_pad: date | None = None
    status_pad: str | None = None


@router.get(
    "",
    response_model=RespostaPaginada[PacienteItem],
    summary="Lista pacientes",
    description="Filtra por operadora. Paginado por offset.",
)
def listar_pacientes(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PaginacaoParams, Depends(paginacao_params)],
    operadora_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RespostaPaginada[PacienteItem]:
    stmt = select(Paciente)
    contagem_stmt = select(func.count()).select_from(Paciente)
    if operadora_id is not None:
        stmt = stmt.where(Paciente.operadora_id == operadora_id)
        contagem_stmt = contagem_stmt.where(Paciente.operadora_id == operadora_id)

    total = session.execute(contagem_stmt).scalar_one()
    linhas = (
        session.execute(stmt.order_by(Paciente.nome).limit(params.limite).offset(params.offset))
        .scalars()
        .all()
    )

    itens = [PacienteItem.model_validate(linha) for linha in linhas]
    return envelope_paginado(itens=itens, total=total, params=params)


@router.post(
    "",
    response_model=PacienteItem,
    status_code=status.HTTP_201_CREATED,
    summary="Cadastra um paciente",
)
def criar_paciente(
    corpo: PacienteCriar,
    session: Annotated[Session, Depends(get_session)],
) -> PacienteItem:
    if session.get(Operadora, corpo.operadora_id) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"operadora {corpo.operadora_id} não encontrada",
        )

    paciente = Paciente(
        nome=corpo.nome,
        operadora_id=corpo.operadora_id,
        modalidade=corpo.modalidade,
        data_vencimento_pad=corpo.data_vencimento_pad,
        status_pad=corpo.status_pad,
    )
    session.add(paciente)
    session.commit()
    session.refresh(paciente)

    return PacienteItem.model_validate(paciente)
