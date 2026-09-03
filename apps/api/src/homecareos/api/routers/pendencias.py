"""`GET /api/pendencias`, `PATCH /api/pendencias/{id}` e `GET /api/pendencias/resumo`.

Nada cria pendências ainda (issue #7, fora desta trilha) — as tabelas ficam
vazias até lá, e isso é o esperado. Nenhum dado de exemplo é inserido aqui.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
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
from homecareos.db.models import Documento, Pendencia, PendenciaStatus
from homecareos.db.session import get_session

router = APIRouter(prefix="/api/pendencias", tags=["pendencias"])

# Transições válidas do ciclo de vida de uma pendência: aberta -> em_correcao
# -> resolvida, sempre para frente, nunca pulando etapa nem voltando.
_TRANSICOES_VALIDAS: dict[PendenciaStatus, frozenset[PendenciaStatus]] = {
    PendenciaStatus.ABERTA: frozenset({PendenciaStatus.EM_CORRECAO}),
    PendenciaStatus.EM_CORRECAO: frozenset({PendenciaStatus.RESOLVIDA}),
    PendenciaStatus.RESOLVIDA: frozenset(),
}

# Janela usada para separar "vencendo em breve" de "futura" no resumo por
# faixa de deadline. Não há especificação de produto para o corte exato;
# 7 dias é a janela operacional mais comum para esse tipo de painel.
_JANELA_PROXIMA = timedelta(days=7)


class PendenciaItem(BaseModel):
    id: uuid.UUID
    documento_id: uuid.UUID
    tipo_problema: str
    descricao: str
    responsavel: str
    status: PendenciaStatus
    deadline: datetime
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class AtualizarPendenciaRequest(BaseModel):
    status: PendenciaStatus


class ResumoPendencias(BaseModel):
    por_status: dict[str, int]
    por_faixa_deadline: dict[str, int]


@router.get(
    "/resumo",
    response_model=ResumoPendencias,
    summary="Contagem de pendências por status e por faixa de deadline",
)
def resumo_pendencias(session: Annotated[Session, Depends(get_session)]) -> ResumoPendencias:
    linhas_status = session.execute(
        select(Pendencia.status, func.count()).group_by(Pendencia.status)
    ).all()
    por_status = {status_enum.value: 0 for status_enum in PendenciaStatus}
    for status_valor, contagem in linhas_status:
        por_status[status_valor.value] = contagem

    agora = datetime.now(UTC)
    limite_proximo = agora + _JANELA_PROXIMA
    em_aberto = Pendencia.status != PendenciaStatus.RESOLVIDA

    vencidas = session.execute(
        select(func.count()).select_from(Pendencia).where(em_aberto, Pendencia.deadline < agora)
    ).scalar_one()
    proximos_7_dias = session.execute(
        select(func.count())
        .select_from(Pendencia)
        .where(em_aberto, Pendencia.deadline >= agora, Pendencia.deadline <= limite_proximo)
    ).scalar_one()
    futuras = session.execute(
        select(func.count())
        .select_from(Pendencia)
        .where(em_aberto, Pendencia.deadline > limite_proximo)
    ).scalar_one()

    return ResumoPendencias(
        por_status=por_status,
        por_faixa_deadline={
            "vencidas": vencidas,
            "proximos_7_dias": proximos_7_dias,
            "futuras": futuras,
        },
    )


@router.get(
    "",
    response_model=RespostaPaginada[PendenciaItem],
    summary="Lista pendências abertas sobre documentos",
    description="Filtra por status, operadora (via documento) e deadline até a data informada.",
)
def listar_pendencias(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PaginacaoParams, Depends(paginacao_params)],
    status_filtro: Annotated[
        PendenciaStatus | None, Query(alias="status", description="Status da pendência")
    ] = None,
    deadline: Annotated[
        date | None, Query(description="Só pendências com deadline até esta data (inclusive)")
    ] = None,
    operadora_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RespostaPaginada[PendenciaItem]:
    stmt = select(Pendencia)
    contagem_stmt = select(func.count()).select_from(Pendencia)
    if operadora_id is not None:
        stmt = stmt.join(Documento, Documento.id == Pendencia.documento_id).where(
            Documento.operadora_id == operadora_id
        )
        contagem_stmt = contagem_stmt.join(Documento, Documento.id == Pendencia.documento_id).where(
            Documento.operadora_id == operadora_id
        )
    if status_filtro is not None:
        stmt = stmt.where(Pendencia.status == status_filtro)
        contagem_stmt = contagem_stmt.where(Pendencia.status == status_filtro)
    if deadline is not None:
        limite = datetime.combine(deadline, time.max, tzinfo=UTC)
        stmt = stmt.where(Pendencia.deadline <= limite)
        contagem_stmt = contagem_stmt.where(Pendencia.deadline <= limite)

    total = session.execute(contagem_stmt).scalar_one()
    linhas = (
        session.execute(
            stmt.order_by(Pendencia.deadline).limit(params.limite).offset(params.offset)
        )
        .scalars()
        .all()
    )

    itens = [PendenciaItem.model_validate(linha) for linha in linhas]
    return envelope_paginado(itens=itens, total=total, params=params)


@router.patch(
    "/{pendencia_id}",
    response_model=PendenciaItem,
    summary="Transiciona o status de uma pendência",
    description="Só aceita a transição para frente: aberta -> em_correcao -> resolvida.",
)
def atualizar_pendencia(
    pendencia_id: uuid.UUID,
    corpo: AtualizarPendenciaRequest,
    session: Annotated[Session, Depends(get_session)],
) -> PendenciaItem:
    pendencia = session.get(Pendencia, pendencia_id)
    if pendencia is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="pendência não encontrada"
        )

    permitidas = _TRANSICOES_VALIDAS.get(pendencia.status, frozenset())
    if corpo.status not in permitidas:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"transição de {pendencia.status.value!r} para {corpo.status.value!r} "
                "não é permitida"
            ),
        )

    pendencia.status = corpo.status
    if corpo.status == PendenciaStatus.RESOLVIDA:
        pendencia.resolved_at = datetime.now(UTC)
    session.commit()
    session.refresh(pendencia)

    return PendenciaItem.model_validate(pendencia)
