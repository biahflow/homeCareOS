"""`POST /api/alertas/varredura` e `GET /api/alertas` — issue #9.

Este router nasce **sem** autenticação própria: a proteção é aplicada em
`main.py` no `include_router(..., dependencies=[Depends(require_api_key)])`,
como para todos os outros — ver a docstring de `api/auth.py` para por que a
regra é por router e nunca endpoint a endpoint.

O provider vem de uma dependency (`obter_provider`) em vez de ser construído
dentro do handler: é o que permite ao teste de integração injetar um dublê em
memória e exercitar a política anti-bombardeio sem tocar em rede.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from homecareos.alerts import repository
from homecareos.alerts.errors import AlertConfigError
from homecareos.alerts.provider import WhatsAppProvider, get_provider
from homecareos.alerts.schema import ResumoVarredura, StatusAlerta, TipoAlerta
from homecareos.alerts.service import executar_varredura
from homecareos.api.pagination import (
    PaginacaoParams,
    RespostaPaginada,
    envelope_paginado,
    paginacao_params,
)
from homecareos.config import Settings, get_settings
from homecareos.db.session import get_session

router = APIRouter(prefix="/api/alertas", tags=["alertas"])


def obter_provider(
    settings: Annotated[Settings, Depends(get_settings)],
) -> WhatsAppProvider | None:
    """Gateway de WhatsApp da requisição; `None` quando não há gateway configurado."""
    return get_provider(settings)


class AlertaItem(BaseModel):
    """Uma linha do log de alertas.

    Expõe `mensagem` — que carrega nome de paciente — pela mesma razão que a
    tabela a guarda: auditar um envio é saber o que foi dito. O endpoint está
    sob `X-API-Key` como todo o resto de `/api/*`.
    """

    id: uuid.UUID
    tipo: str
    chave: str
    destinatario: str
    mensagem: str
    status: str
    detalhe: str | None
    documento_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post(
    "/varredura",
    response_model=ResumoVarredura,
    summary="Roda os detectores de alerta e envia o que for novo",
    description=(
        "Idempotente na prática: o cooldown impede que duas varreduras seguidas "
        "avisem duas vezes do mesmo assunto. É o mesmo trabalho que o "
        "`python -m homecareos.alerts.scan` do cron faz."
    ),
)
def varredura(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    provider: Annotated[WhatsAppProvider | None, Depends(obter_provider)],
) -> ResumoVarredura:
    try:
        return executar_varredura(session, settings, provider)
    except AlertConfigError as exc:
        # 422 com a mensagem inteira: é erro de configuração de quem opera, e a
        # mensagem tem que dizer o que consertar. Sem ela, a pessoa recebe "erro
        # de configuração" e volta a caçar o typo no `.env` no escuro.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get(
    "",
    response_model=RespostaPaginada[AlertaItem],
    summary="Log de alertas enviados",
    description=(
        "Do mais recente para o mais antigo. Filtra por tipo, status "
        "(`enviado`/`falha`/`suprimido`) e documento."
    ),
)
def listar_alertas(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PaginacaoParams, Depends(paginacao_params)],
    tipo: Annotated[TipoAlerta | None, Query(description="Tipo do alerta")] = None,
    status_filtro: Annotated[
        StatusAlerta | None, Query(alias="status", description="Desfecho do envio")
    ] = None,
    documento_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RespostaPaginada[AlertaItem]:
    linhas, total = repository.listar(
        session,
        tipo=tipo,
        status=status_filtro,
        documento_id=documento_id,
        limite=params.limite,
        offset=params.offset,
    )
    itens = [AlertaItem.model_validate(linha) for linha in linhas]
    return envelope_paginado(itens=itens, total=total, params=params)
