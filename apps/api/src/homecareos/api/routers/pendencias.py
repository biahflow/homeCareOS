"""`GET /api/pendencias`, `PATCH /api/pendencias/{id}` e `GET /api/pendencias/resumo`.

Quem cria pendências é a classificação (`homecareos.classification.service`),
nunca este router: aqui a equipe só as transiciona. O `PATCH` é o ponto em que
o ciclo de correção avança, e por isso ele propaga a transição da pendência
para o documento — inclusive disparando a revalidação quando a última pendência
do documento é resolvida.
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
from homecareos.classification.errors import RevalidacaoIndisponivelError
from homecareos.classification.service import (
    registrar_log,
    revalidar_documento,
    transicionar,
)
from homecareos.db.models import Documento, DocumentoStatus, Pendencia, PendenciaStatus
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
    # Nulo em pendência anterior à classificação automática (issue #7).
    campo: str | None
    descricao: str
    responsavel: str
    status: PendenciaStatus
    deadline: datetime
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class AtualizarPendenciaRequest(BaseModel):
    status: PendenciaStatus
    # Opcional: fecha o passo "pendência é atribuída a um responsável" do ciclo,
    # sem tornar obrigatório repetir o responsável a cada transição. `status`
    # continua obrigatório — o PATCH é, antes de tudo, uma transição.
    responsavel: str | None = None


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
    if corpo.responsavel is not None:
        pendencia.responsavel = corpo.responsavel
    if corpo.status == PendenciaStatus.RESOLVIDA:
        pendencia.resolved_at = datetime.now(UTC)
    session.commit()

    _propagar_para_o_documento(session, pendencia)
    session.refresh(pendencia)

    return PendenciaItem.model_validate(pendencia)


def _propagar_para_o_documento(session: Session, pendencia: Pendencia) -> None:
    """Reflete no documento a transição que acabou de acontecer na pendência.

    A pendência já foi commitada antes desta chamada, e de propósito: a
    transição é do usuário e não pode ser desfeita por uma falha do
    encadeamento automático que vem depois dela.
    """
    documento = session.get(Documento, pendencia.documento_id)
    if documento is None:  # FK garante que não acontece; guarda para o mypy
        return

    if pendencia.status is PendenciaStatus.EM_CORRECAO:
        # Só a primeira pendência a entrar em correção move o documento; da
        # segunda em diante ele já está em `em_correcao` e não há o que fazer.
        if documento.status in {DocumentoStatus.PROBLEMA, DocumentoStatus.INCOMPLETO}:
            transicionar(
                session,
                documento,
                DocumentoStatus.EM_CORRECAO,
                usuario="api",
                detalhe=f"pendência {pendencia.id} entrou em correção",
            )
            session.commit()
        return

    if pendencia.status is not PendenciaStatus.RESOLVIDA:
        return

    pendentes = session.execute(
        select(func.count())
        .select_from(Pendencia)
        .where(
            Pendencia.documento_id == documento.id,
            Pendencia.status != PendenciaStatus.RESOLVIDA,
        )
    ).scalar_one()
    if pendentes > 0:
        return
    # Fora de `em_correcao` não há correção em curso para concluir (pendência
    # criada à mão, documento ainda em `processando`): não há o que propagar.
    if documento.status is not DocumentoStatus.EM_CORRECAO:
        return

    transicionar(
        session,
        documento,
        DocumentoStatus.RESOLVIDO,
        usuario="api",
        detalhe="todas as pendências do documento foram resolvidas",
    )
    session.commit()

    try:
        revalidar_documento(session, documento.id, usuario="api")
    except RevalidacaoIndisponivelError as exc:
        # Nunca 500: a transição da pendência é do usuário e já aconteceu. Se a
        # revalidação automática não tem insumo (documento sem extração, sem
        # operadora, operadora sem regra ativa), o documento fica em `resolvido`
        # esperando ação humana e o motivo fica registrado para quem for olhar.
        registrar_log(
            session,
            documento_id=documento.id,
            acao="revalidacao:indisponivel",
            usuario="api",
            detalhe=str(exc),
        )
        session.commit()
