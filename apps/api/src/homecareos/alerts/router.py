"""`POST /api/alertas/varredura` e `GET /api/alertas` — issue #9.

Este router nasce **sem** autenticação própria: a proteção é aplicada em
`main.py` no `include_router(...)`, como para todos os outros — ver a docstring
de `api/auth.py` para por que a regra é por router e nunca endpoint a endpoint.
Desde a issue #30 a regra deste router é `exigir_papel(coordenador, gestor)`:
disparar varredura e ler quem foi notificado é acompanhamento da operação, não
execução dela. A chave de API continua passando, como em todo o resto.

Os canais vêm de uma dependency (`obter_canais`) em vez de serem construídos
dentro do handler: é o que permite ao teste de integração injetar dublês em
memória e exercitar a política anti-bombardeio sem tocar em rede nem em caixa
postal.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from homecareos.alerts import repository
from homecareos.alerts.canais import CanalAlerta, construir_canais
from homecareos.alerts.errors import AlertConfigError
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
from homecareos.limites.dependencies import limitar
from homecareos.limites.schema import Recurso

router = APIRouter(prefix="/api/alertas", tags=["alertas"])


def _erro_de_configuracao(exc: AlertConfigError) -> HTTPException:
    """422 com a mensagem inteira: é erro de configuração de quem opera, e a
    mensagem tem que dizer o que consertar. Sem ela, a pessoa recebe "erro de
    configuração" e volta a caçar o typo no `.env` no escuro.

    Existe como função porque a configuração é lida em **dois** momentos da
    mesma requisição — na dependency, para montar os canais, e dentro da
    varredura, para os destinatários e templates —, e um typo tem de virar o
    mesmo 422 nos dois. Enquanto isto era só o corpo do handler, um
    `ALERTAS_CANAIS` inválido escapava pela dependency e virava 500.
    """
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def obter_canais(
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[CanalAlerta]:
    """Os canais desta requisição, cada um sabendo se está ligado e se tem credencial.

    Devolve **todos** os canais implementados, não só os que enviam: o resumo
    da varredura precisa distinguir "desliguei este canal" de "liguei e esqueci
    a credencial" (ADR 0006).
    """
    try:
        return construir_canais(settings)
    except AlertConfigError as exc:
        # Dependency roda ANTES do corpo do handler: sem este `except`, o
        # `try` de `varredura` nunca veria o erro e um `ALERTAS_CANAIS` com
        # typo viraria 500 em vez do 422 que diz o que consertar.
        raise _erro_de_configuracao(exc) from exc


class AlertaItem(BaseModel):
    """Uma linha do log de alertas.

    Expõe `mensagem` — que carrega nome de paciente — pela mesma razão que a
    tabela a guarda: auditar um envio é saber o que foi dito. O endpoint exige
    credencial como todo o resto de `/api/*`, e papel `coordenador` ou `gestor`
    quando quem chama é uma pessoa.
    """

    id: uuid.UUID
    tipo: str
    canal: str
    """Por onde a mensagem saiu (`whatsapp`/`email`). Sem ele, duas linhas do
    mesmo aviso para a mesma pessoa seriam indistinguíveis no log — e é
    justamente isso que o segundo canal produz de propósito (ADR 0006)."""

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
    # Rate limit por identidade (ADR 0005): a varredura dispara os detectores e
    # fala com os gateways dos canais ligados, enviando mensagem de verdade. O cron de
    # produção NÃO passa por aqui — ele chama `python -m homecareos.alerts.scan`,
    # o módulo, que não faz requisição nenhuma —, mas a chave de máquina ganha
    # limite folgado mesmo assim: nada garante que alguém não tenha apontado um
    # agendador para esta rota.
    dependencies=[Depends(limitar(Recurso.VARREDURA_ALERTAS))],
    responses={429: {"description": "Limite de varreduras por hora atingido para esta identidade"}},
)
def varredura(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    canais: Annotated[list[CanalAlerta], Depends(obter_canais)],
) -> ResumoVarredura:
    try:
        return executar_varredura(session, settings, canais)
    except AlertConfigError as exc:
        raise _erro_de_configuracao(exc) from exc


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
