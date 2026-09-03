"""`GET /api/operadoras`.

Lista simples, sem paginação: o número de operadoras é pequeno (convênios
atendidos pela empresa, não um cadastro de alto volume) e o spec desta
trilha não pede paginação para este endpoint especificamente (diferente de
documentos/pendências/pacientes).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from homecareos.db.models import Operadora
from homecareos.db.session import get_session

router = APIRouter(prefix="/api/operadoras", tags=["operadoras"])


class OperadoraItem(BaseModel):
    id: uuid.UUID
    nome: str
    codigo: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get(
    "",
    response_model=list[OperadoraItem],
    summary="Lista as operadoras cadastradas",
)
def listar_operadoras(session: Annotated[Session, Depends(get_session)]) -> list[OperadoraItem]:
    linhas = session.execute(select(Operadora).order_by(Operadora.nome)).scalars().all()
    return [OperadoraItem.model_validate(linha) for linha in linhas]
