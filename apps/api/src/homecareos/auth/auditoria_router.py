"""`GET /api/usuarios/auditoria` — leitura paginada da auditoria administrativa (issue #30).

Router **separado** de `auth.usuarios_router`, e a escolha é deliberada. As
três rotas de `usuarios_router` são descritas ali e no `apps/api/README.md`
como "três rotas, e nenhuma mais" — um limite de superfície que existe porque
aquele é o endpoint mais perigoso da API (quem cria usuário decide quem
entra). Diluir esse invariante com uma rota de leitura, ainda que inofensiva,
custaria a legibilidade desse limite sem ganhar nada: a auditoria é dado
histórico e append-only, um ciclo de vida diferente do CRUD de conta. Vive sob
o mesmo prefixo (`/api/usuarios/auditoria`) porque semanticamente é dela que
trata, mas como router e arquivo próprios.

A autorização é a mesma dos dados que ela expõe: só o **coordenador**, o papel
que administra usuário (ADR 0004) — aplicada em `main.py`, no
`include_router(...)`, como todo o resto de `/api/*`. `X-API-Key` passa por
`exigir_papel` em qualquer papel, como em qualquer outra rota (ver
`auth/dependencies.exigir_papel`).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from homecareos.api.pagination import (
    PaginacaoParams,
    RespostaPaginada,
    envelope_paginado,
    paginacao_params,
)
from homecareos.auth.schema import AcaoAuditoriaUsuario, AuditoriaUsuarioOut
from homecareos.db.models import AuditoriaUsuario
from homecareos.db.session import get_session

router = APIRouter(prefix="/api/usuarios/auditoria", tags=["usuarios"])


@router.get(
    "",
    response_model=RespostaPaginada[AuditoriaUsuarioOut],
    summary="Lista a auditoria administrativa de usuários",
    description=(
        "Quem criou, alterou, desativou ou reativou qual usuário, e quando. "
        "Paginado, do evento mais recente para o mais antigo. Filtra por "
        "`alvo_id` (quem sofreu a ação), `ator_id` (quem agiu) e `acao`. Nunca "
        "devolve `senha_hash`, `mfa_secret`, `mfa_ultimo_passo` nem token "
        "nenhum."
    ),
)
def listar_auditoria(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PaginacaoParams, Depends(paginacao_params)],
    alvo_id: Annotated[uuid.UUID | None, Query(description="Só eventos sobre este usuário")] = None,
    ator_id: Annotated[
        uuid.UUID | None, Query(description="Só eventos feitos por este usuário")
    ] = None,
    acao: Annotated[AcaoAuditoriaUsuario | None, Query(description="Só eventos deste tipo")] = None,
) -> RespostaPaginada[AuditoriaUsuarioOut]:
    """Igual e-mail em `listar_usuarios`, o e-mail do alvo aparece aqui de propósito.

    Quem lê esta rota é o mesmo coordenador que já vê o e-mail de todo mundo em
    `GET /api/usuarios` — não há exposição nova, e escondê-lo tornaria a linha
    inútil meses depois (ver a docstring de `db/models/auditoria_usuario.py`).
    """
    stmt = select(AuditoriaUsuario)
    contagem_stmt = select(func.count()).select_from(AuditoriaUsuario)
    if alvo_id is not None:
        stmt = stmt.where(AuditoriaUsuario.alvo_usuario_id == alvo_id)
        contagem_stmt = contagem_stmt.where(AuditoriaUsuario.alvo_usuario_id == alvo_id)
    if ator_id is not None:
        stmt = stmt.where(AuditoriaUsuario.usuario_id == ator_id)
        contagem_stmt = contagem_stmt.where(AuditoriaUsuario.usuario_id == ator_id)
    if acao is not None:
        stmt = stmt.where(AuditoriaUsuario.acao == acao.value)
        contagem_stmt = contagem_stmt.where(AuditoriaUsuario.acao == acao.value)

    total = session.execute(contagem_stmt).scalar_one()
    linhas = (
        session.execute(
            # Desempate por `id`: `created_at` sozinho empata sempre que duas
            # mudanças acontecem no mesmo instante (dois `PATCH` na mesma
            # transação de um script, por exemplo), e sem desempate a
            # paginação por offset duplica ou pula linha — o mesmo motivo do
            # `(nome, email)` de `listar_usuarios`.
            stmt.order_by(AuditoriaUsuario.created_at.desc(), AuditoriaUsuario.id.desc())
            .limit(params.limite)
            .offset(params.offset)
        )
        .scalars()
        .all()
    )

    itens = [AuditoriaUsuarioOut.model_validate(linha) for linha in linhas]
    return envelope_paginado(itens=itens, total=total, params=params)
