"""`GET /api/documentos`, `GET /api/documentos/{id}` e `POST .../revalidar`.

`POST /api/documentos` **não** mora aqui — continua em
`homecareos.intake.router`, que esta trilha não toca (é o contrato já
consumido pelo frontend).

A revalidação é o único endpoint daqui que escreve: ela reaplica as regras
ativas sobre a última extração já existente e reclassifica o documento. Toda a
lógica vive em `homecareos.classification.service`; este módulo só traduz os
erros de domínio em status HTTP.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

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
from homecareos.classification.errors import (
    DocumentoNaoEncontradoError,
    RevalidacaoIndisponivelError,
    TransicaoInvalidaError,
)
from homecareos.classification.service import revalidar_documento as revalidar
from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    Extracao,
    Pendencia,
    PendenciaStatus,
    ResultadoValidacao,
    TipoDocumento,
    Validacao,
)
from homecareos.db.session import get_session

router = APIRouter(prefix="/api/documentos", tags=["documentos"])


class DocumentoListItem(BaseModel):
    """Um documento na listagem — sem extração/validações (ver detalhe)."""

    id: uuid.UUID
    tipo: TipoDocumento
    competencia: str
    status: DocumentoStatus
    pagina: int | None
    paciente_id: uuid.UUID | None
    operadora_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ExtracaoResumo(BaseModel):
    id: uuid.UUID
    campos_extraidos: dict[str, Any]
    confianca: float
    confianca_por_campo: dict[str, Any]
    modelo: str
    provider: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ValidacaoResumo(BaseModel):
    id: uuid.UUID
    regra_id: uuid.UUID
    resultado: ResultadoValidacao
    detalhe: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentoDetalhe(DocumentoListItem):
    """Detalhe de um documento: os campos da listagem, mais extração e validações."""

    arquivo_url: str
    extracao: ExtracaoResumo | None
    validacoes: list[ValidacaoResumo]


@router.get(
    "",
    response_model=RespostaPaginada[DocumentoListItem],
    summary="Lista documentos em conferência",
    description="Filtra por competência, status, operadora e paciente. Paginado por offset.",
)
def listar_documentos(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PaginacaoParams, Depends(paginacao_params)],
    competencia: Annotated[str | None, Query(description="Competência `YYYY-MM`")] = None,
    status_filtro: Annotated[
        DocumentoStatus | None, Query(alias="status", description="Status do documento")
    ] = None,
    operadora_id: Annotated[uuid.UUID | None, Query()] = None,
    paciente_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RespostaPaginada[DocumentoListItem]:
    filtros = []
    if competencia is not None:
        filtros.append(Documento.competencia == competencia)
    if status_filtro is not None:
        filtros.append(Documento.status == status_filtro)
    if operadora_id is not None:
        filtros.append(Documento.operadora_id == operadora_id)
    if paciente_id is not None:
        filtros.append(Documento.paciente_id == paciente_id)

    total = session.execute(
        select(func.count()).select_from(Documento).where(*filtros)
    ).scalar_one()
    linhas = (
        session.execute(
            select(Documento)
            .where(*filtros)
            .order_by(Documento.created_at.desc())
            .limit(params.limite)
            .offset(params.offset)
        )
        .scalars()
        .all()
    )

    itens = [DocumentoListItem.model_validate(linha) for linha in linhas]
    return envelope_paginado(itens=itens, total=total, params=params)


@router.get(
    "/{documento_id}",
    response_model=DocumentoDetalhe,
    summary="Detalhe de um documento",
    description="Documento com a extração (quando concluída) e as validações já aplicadas.",
)
def obter_documento(
    documento_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> DocumentoDetalhe:
    documento = session.get(Documento, documento_id)
    if documento is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="documento não encontrado"
        )

    extracao = (
        session.execute(select(Extracao).where(Extracao.documento_id == documento_id))
        .scalars()
        .first()
    )
    validacoes = (
        session.execute(select(Validacao).where(Validacao.documento_id == documento_id))
        .scalars()
        .all()
    )

    return DocumentoDetalhe(
        **DocumentoListItem.model_validate(documento).model_dump(),
        arquivo_url=documento.arquivo_url,
        extracao=ExtracaoResumo.model_validate(extracao) if extracao is not None else None,
        validacoes=[ValidacaoResumo.model_validate(validacao) for validacao in validacoes],
    )


class RevalidacaoResponse(BaseModel):
    """Resultado de uma revalidação: onde o documento parou e quanto ainda falta."""

    documento_id: uuid.UUID
    status: DocumentoStatus
    pendencias_abertas: int


@router.post(
    "/{documento_id}/revalidar",
    response_model=RevalidacaoResponse,
    summary="Revalida um documento contra as regras ativas da operadora",
    description=(
        "Reaplica as regras ativas sobre a última extração já registrada e "
        "reclassifica o documento. Não chama o provider de extração de novo."
    ),
)
def revalidar_documento(
    documento_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> RevalidacaoResponse:
    try:
        status_final = revalidar(session, documento_id, usuario="api")
    except DocumentoNaoEncontradoError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (RevalidacaoIndisponivelError, TransicaoInvalidaError) as exc:
        # 409 e não 422: o corpo da requisição está correto — é o estado atual
        # do documento (sem operadora, sem extração, já terminal) que impede a
        # revalidação agora.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    pendencias_abertas = session.execute(
        select(func.count())
        .select_from(Pendencia)
        .where(
            Pendencia.documento_id == documento_id,
            Pendencia.status != PendenciaStatus.RESOLVIDA,
        )
    ).scalar_one()

    return RevalidacaoResponse(
        documento_id=documento_id,
        status=status_final,
        pendencias_abertas=pendencias_abertas,
    )
