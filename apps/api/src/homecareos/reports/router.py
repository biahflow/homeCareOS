"""`GET /api/relatorios/*` e `PUT /api/relatorios/baseline` — issue #8.

Dois produtos sobre o mesmo dado, com públicos diferentes: o relatório
operacional (`/conferencia` e `/conferencia.csv`), que a conferente usa todo
dia, e as métricas agregadas (`/metricas`), que sustentam a comparação
antes/depois. O `/baseline` é o cadastro que alimenta a segunda.

A proteção base é aplicada em `main.py`, no `include_router(...)`, como para
todos os outros — ver a docstring de `api/auth.py` para por que a regra é por
router e nunca endpoint a endpoint.

Este router é **exceção consciente** a essa regra desde a issue #30, e a razão
é o público misto do primeiro parágrafo: o relatório operacional é dos três
papéis, as métricas são de coordenador e gestor, e o baseline é escrito só pelo
gestor (é dado de gestão, não de conferência — ADR 0001). Uma regra única no
router ou fecharia o relatório para quem o usa todo dia, ou abriria o baseline
para quem não o escreve. Por isso cada endpoint restrito declara o seu papel, e
a regra do router (os três) continua valendo por baixo.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from homecareos.api.pagination import (
    PaginacaoParams,
    RespostaPaginada,
    envelope_paginado,
    paginacao_params,
)
from homecareos.auth.dependencies import exigir_papel
from homecareos.auth.schema import Papel
from homecareos.db.models import BaselineCompetencia, DocumentoStatus, Operadora
from homecareos.db.session import get_session, get_sessionmaker
from homecareos.limites.dependencies import limitar
from homecareos.limites.schema import Recurso
from homecareos.reports import csv_export
from homecareos.reports.conferencia import (
    FiltroConferencia,
    contar_documentos,
    iterar_paginas,
    montar_linhas,
)
from homecareos.reports.metricas import calcular_metricas
from homecareos.reports.schema import (
    PADRAO_COMPETENCIA,
    BaselineOut,
    BaselineUpsert,
    LinhaConferencia,
    MetricasResponse,
)

router = APIRouter(prefix="/api/relatorios", tags=["relatorios"])

_COMPETENCIA = re.compile(PADRAO_COMPETENCIA)


def _validar_competencia(valor: str | None, *, campo: str) -> str | None:
    """Recusa competência fora de `YYYY-MM` antes de ela virar filtro.

    Sem esta validação um `2026-13` viraria simplesmente uma consulta que não
    casa com nada, e quem chamou receberia "nenhum documento" — indistinguível
    de uma competência real e vazia.
    """
    if valor is None or _COMPETENCIA.match(valor):
        return valor
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"{campo} inválida: use o formato AAAA-MM (por exemplo, 2026-08)",
    )


def filtro_conferencia(
    competencia: Annotated[str | None, Query(description="Competência `AAAA-MM`")] = None,
    status_filtro: Annotated[
        DocumentoStatus | None, Query(alias="status", description="Status atual do documento")
    ] = None,
    operadora_id: Annotated[uuid.UUID | None, Query()] = None,
    paciente_id: Annotated[uuid.UUID | None, Query()] = None,
    data_inicio: Annotated[
        date | None, Query(description="Recebidos a partir desta data (inclusive)")
    ] = None,
    data_fim: Annotated[
        date | None, Query(description="Recebidos até esta data (inclusive)")
    ] = None,
    apenas_pendentes: Annotated[
        bool, Query(description="Só documentos com pendência não resolvida")
    ] = False,
) -> FiltroConferencia:
    """Dependency compartilhada pelo JSON e pelo CSV — o CSV é o mesmo filtro sem página."""
    return FiltroConferencia(
        competencia=_validar_competencia(competencia, campo="competencia"),
        status=status_filtro,
        operadora_id=operadora_id,
        paciente_id=paciente_id,
        data_inicio=data_inicio,
        data_fim=data_fim,
        apenas_pendentes=apenas_pendentes,
    )


@router.get(
    "/conferencia",
    response_model=RespostaPaginada[LinhaConferencia],
    summary="Relatório de conferência da competência",
    description=(
        "Uma linha por documento, com o problema encontrado e a ação necessária. "
        "Ordenado pelo que precisa de ação humana primeiro (incompleto, problema, "
        "em correção), depois pelo prazo mais próximo."
    ),
)
def relatorio_conferencia(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PaginacaoParams, Depends(paginacao_params)],
    filtro: Annotated[FiltroConferencia, Depends(filtro_conferencia)],
) -> RespostaPaginada[LinhaConferencia]:
    total = contar_documentos(session, filtro)
    linhas = montar_linhas(session, filtro, limite=params.limite, offset=params.offset)
    return envelope_paginado(itens=linhas, total=total, params=params)


def _stream_csv(filtro: FiltroConferencia) -> Iterator[str]:
    """Gera o CSV com uma sessão própria, aberta e fechada dentro do gerador.

    A sessão de `Depends(get_session)` é encerrada quando o handler retorna — ou
    seja, antes de o corpo da resposta ser transmitido. Um `StreamingResponse`
    que dependesse dela estaria lendo o banco por uma sessão já fechada.
    """
    with get_sessionmaker()() as session:
        yield from csv_export.gerar_csv(iterar_paginas(session, filtro))


@router.get(
    "/conferencia.csv",
    summary="Relatório de conferência em CSV (abre no Excel pt-BR)",
    description=(
        "Mesmos filtros de `/conferencia`, sem paginação: o CSV é o extrato "
        "inteiro do filtro, transmitido em blocos."
    ),
    response_class=StreamingResponse,
    # Rate limit por identidade (ADR 0005): é o extrato inteiro do filtro, sem
    # o teto de `limite <= 200` que segura as leituras paginadas.
    dependencies=[Depends(limitar(Recurso.RELATORIO_CSV))],
    responses={
        429: {"description": "Limite de exportações por hora atingido para esta identidade"}
    },
)
def relatorio_conferencia_csv(
    filtro: Annotated[FiltroConferencia, Depends(filtro_conferencia)],
) -> StreamingResponse:
    nome = csv_export.nome_arquivo(filtro.competencia, datetime.now(UTC).date())
    return StreamingResponse(
        _stream_csv(filtro),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@router.get(
    "/metricas",
    response_model=MetricasResponse,
    # Métrica agregada é leitura de gestão, não de conferência (ver a docstring
    # do módulo para a exceção à regra "auth por router").
    dependencies=[Depends(exigir_papel(Papel.COORDENADOR, Papel.GESTOR))],
    summary="Métricas agregadas por competência, operadora e dia",
    description=(
        "O que o sistema mediu (pendência detectada antes do envio) e o que foi "
        "informado à mão (glosa) aparecem lado a lado e nomeados, nunca fundidos "
        "num número único."
    ),
)
def metricas(
    session: Annotated[Session, Depends(get_session)],
    competencia_inicio: Annotated[str | None, Query(description="Competência `AAAA-MM`")] = None,
    competencia_fim: Annotated[str | None, Query(description="Competência `AAAA-MM`")] = None,
    operadora_id: Annotated[uuid.UUID | None, Query()] = None,
) -> MetricasResponse:
    return calcular_metricas(
        session,
        competencia_inicio=_validar_competencia(competencia_inicio, campo="competencia_inicio"),
        competencia_fim=_validar_competencia(competencia_fim, campo="competencia_fim"),
        operadora_id=operadora_id,
    )


@router.put(
    "/baseline",
    response_model=BaselineOut,
    # Só o gestor escreve baseline: é o número de glosa informado pela
    # operadora, a régua contra a qual o próprio sistema é medido. Quem opera a
    # conferência não deve poder mexer na régua que mede a conferência.
    dependencies=[Depends(exigir_papel(Papel.GESTOR))],
    summary="Registra ou corrige o baseline de glosa de uma competência",
    description=(
        "Upsert idempotente pela chave natural `(competencia, operadora_id)`. "
        "`operadora_id` ausente é o consolidado de todas as operadoras."
    ),
)
def upsert_baseline(
    corpo: BaselineUpsert,
    session: Annotated[Session, Depends(get_session)],
) -> BaselineOut:
    """`PUT` e não `POST`: baseline é digitado à mão e é corrigido.

    Um `POST` que respondesse 409 na segunda tentativa obrigaria a operação a
    descobrir o `id` da linha antes de consertar um número errado — atrito puro
    sobre um cadastro que existe justamente para ser revisado.

    O upsert é um `INSERT ... ON CONFLICT DO UPDATE`, e não um "procura, se não
    achou insere": este último tem janela de corrida entre a leitura e a
    escrita, e dois `PUT` simultâneos na mesma chave natural terminariam com o
    segundo batendo no índice único e virando 500 para quem só queria corrigir
    um número.
    """
    # A FK sozinha devolveria um `IntegrityError` cru; quem digitou o id errado
    # precisa ler o que aconteceu.
    if corpo.operadora_id is not None and session.get(Operadora, corpo.operadora_id) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="operadora não encontrada",
        )

    inserir = insert(BaselineCompetencia).values(
        competencia=corpo.competencia,
        operadora_id=corpo.operadora_id,
        documentos_enviados=corpo.documentos_enviados,
        documentos_glosados=corpo.documentos_glosados,
        valor_glosado_centavos=corpo.valor_glosado_centavos,
        horas_conferencia=corpo.horas_conferencia,
        fonte=corpo.fonte,
        observacao=corpo.observacao,
    )
    atualizar: dict[str, Any] = {
        campo: inserir.excluded[campo]
        for campo in (
            "documentos_enviados",
            "documentos_glosados",
            "valor_glosado_centavos",
            "horas_conferencia",
            "fonte",
            "observacao",
        )
    }
    # `updated_at` explicitamente: o `onupdate` do model só dispara em UPDATE
    # emitido pelo ORM, e este é um INSERT ... ON CONFLICT em Core. Sem esta
    # linha a correção de um número deixaria o carimbo de tempo mentindo.
    atualizar["updated_at"] = func.now()

    # O alvo do ON CONFLICT precisa casar com o índice certo, e são dois: o
    # consolidado (`operadora_id IS NULL`) só é coberto pelo índice PARCIAL,
    # porque no Postgres dois `NULL` não colidem no índice comum. Apontar
    # sempre para `(competencia, operadora_id)` faria o caso consolidado
    # escapar da cláusula e continuar estourando no índice parcial.
    if corpo.operadora_id is None:
        stmt = inserir.on_conflict_do_update(
            index_elements=["competencia"],
            index_where=BaselineCompetencia.operadora_id.is_(None),
            set_=atualizar,
        )
    else:
        stmt = inserir.on_conflict_do_update(
            index_elements=["competencia", "operadora_id"], set_=atualizar
        )

    baseline = session.scalars(stmt.returning(BaselineCompetencia)).one()
    session.commit()

    return BaselineOut.model_validate(baseline)


@router.get(
    "/baseline",
    response_model=list[BaselineOut],
    summary="Lista os baselines de glosa registrados",
    # Leitura de gestão, como `/metricas`.
    dependencies=[Depends(exigir_papel(Papel.COORDENADOR, Papel.GESTOR))],
)
def listar_baselines(session: Annotated[Session, Depends(get_session)]) -> list[BaselineOut]:
    """Sem paginação: é um cadastro pequeno (uma linha por competência/operadora).

    Mesma justificativa que `api/routers/operadoras.py` já registra.
    """
    linhas = session.scalars(
        select(BaselineCompetencia)
        .outerjoin(Operadora, Operadora.id == BaselineCompetencia.operadora_id)
        .order_by(BaselineCompetencia.competencia, Operadora.nome)
    )
    return [BaselineOut.model_validate(linha) for linha in linhas]
