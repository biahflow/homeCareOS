"""Montagem do relatório operacional de conferência — a lista que a conferente lê todo dia.

Uma linha por documento da competência, com o problema encontrado e a ação
necessária já resolvidos no backend. As regras de severidade e de ação são
decisão de produto e vivem aqui como funções puras (`severidade_de`,
`acao_necessaria`), testadas sem banco: elas não podem ser reimplementadas no
frontend nem duplicadas no exportador de CSV.

O ponto crítico de implementação é o **N+1**: uma competência de fechamento tem
milhares de documentos, e uma consulta de pendência por documento transformaria
o relatório do dia num timeout. A montagem faz, por página, exatamente quatro
consultas — documentos (já com nomes de paciente e operadora), contagem,
pendências não resolvidas da página e extrações da página.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import ColumnElement, case, exists, func, select
from sqlalchemy.orm import Session

from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    Extracao,
    Operadora,
    Paciente,
    Pendencia,
    PendenciaStatus,
)
from homecareos.reports.schema import LinhaConferencia, Severidade

# Ordem de exibição do relatório. Não é o ciclo de vida do documento: é "o que
# precisa de ação humana primeiro". `incompleto` abre a lista porque é o
# documento que precisa voltar para o campo (o prazo mais longo de todos), e
# `aprovado` fecha porque não há nada a fazer com ele.
PRIORIDADE_STATUS: dict[DocumentoStatus, int] = {
    DocumentoStatus.INCOMPLETO: 0,
    DocumentoStatus.PROBLEMA: 1,
    DocumentoStatus.EM_CORRECAO: 2,
    DocumentoStatus.RESOLVIDO: 3,
    DocumentoStatus.PROCESSANDO: 4,
    DocumentoStatus.LIBERADO: 5,
    DocumentoStatus.APROVADO: 6,
}

# Mapa status -> severidade. `processando` é `OK` de propósito: ainda não há
# veredito nenhum sobre o documento, e pintar de vermelho o que só está na fila
# treinaria a conferente a ignorar vermelho.
_SEVERIDADE_POR_STATUS: dict[DocumentoStatus, Severidade] = {
    DocumentoStatus.INCOMPLETO: Severidade.CRITICO,
    DocumentoStatus.PROBLEMA: Severidade.ATENCAO,
    DocumentoStatus.EM_CORRECAO: Severidade.ATENCAO,
    DocumentoStatus.RESOLVIDO: Severidade.ATENCAO,
    DocumentoStatus.APROVADO: Severidade.OK,
    DocumentoStatus.LIBERADO: Severidade.OK,
    DocumentoStatus.PROCESSANDO: Severidade.OK,
}

# Status em que não há mais nada a fazer com o documento — e portanto nenhum
# prazo a anunciar na ação necessária.
_STATUS_TERMINAIS = frozenset({DocumentoStatus.APROVADO, DocumentoStatus.LIBERADO})

# Tamanho da página usada pelo exportador de CSV. Não é o limite da API: o CSV
# é o extrato inteiro do filtro, percorrido em blocos para nunca carregar a
# competência toda em memória.
TAMANHO_PAGINA_EXPORTACAO = 500

# Menor deadline entre as pendências não resolvidas do documento, correlacionado
# com a linha de `documentos`. Serve só à ordenação: o valor devolvido na linha
# sai do mesmo lote de pendências que produz `problema_encontrado`, para os três
# campos nunca discordarem entre si.
_MENOR_DEADLINE_ABERTO = (
    select(func.min(Pendencia.deadline))
    .where(
        Pendencia.documento_id == Documento.id,
        Pendencia.status != PendenciaStatus.RESOLVIDA,
    )
    .correlate(Documento)
    .scalar_subquery()
)

_ORDEM_POR_PRIORIDADE = case(
    PRIORIDADE_STATUS, value=Documento.status, else_=len(PRIORIDADE_STATUS)
)


@dataclass(frozen=True)
class FiltroConferencia:
    """Os filtros do relatório, compartilhados pelo JSON paginado e pelo CSV."""

    competencia: str | None = None
    status: DocumentoStatus | None = None
    operadora_id: uuid.UUID | None = None
    paciente_id: uuid.UUID | None = None
    data_inicio: date | None = None
    data_fim: date | None = None
    apenas_pendentes: bool = False


def severidade_de(status: DocumentoStatus) -> Severidade:
    """Traduz o status do documento na cor do painel (ver `_SEVERIDADE_POR_STATUS`)."""
    return _SEVERIDADE_POR_STATUS[status]


def acao_necessaria(
    status: DocumentoStatus, pendencias_abertas: int, deadline: datetime | None
) -> str:
    """Frase imperativa do que fazer com o documento, pronta para o painel e para o CSV.

    O prazo só entra quando existe e quando o documento ainda tem o que fazer:
    anunciar prazo num documento já liberado é ruído que compete com a linha que
    de fato precisa de ação.
    """
    if status is DocumentoStatus.INCOMPLETO:
        texto = f"Documento volta para o campo: {pendencias_abertas} pendência(s) a corrigir."
    elif status is DocumentoStatus.PROBLEMA:
        texto = f"Conferir antes do envio: {pendencias_abertas} pendência(s)."
    elif status is DocumentoStatus.EM_CORRECAO:
        texto = f"Correção em andamento: {pendencias_abertas} pendência(s)."
    elif status is DocumentoStatus.RESOLVIDO:
        texto = "Revalidar para liberar."
    elif status is DocumentoStatus.PROCESSANDO:
        texto = "Aguardando extração e classificação."
    else:
        texto = "Nenhuma."

    if deadline is not None and status not in _STATUS_TERMINAIS:
        texto += f" Prazo: {deadline.astimezone(UTC):%d/%m/%Y}."
    return texto


def data_atendimento_de(campos_extraidos: dict[str, Any] | None) -> date | None:
    """Lê `data_atendimento` de `extracoes.campos_extraidos`, tolerando lixo.

    `campos_extraidos` é JSONB livre preenchido por um provider de Vision sobre
    uma foto de qualidade ruim: o campo pode vir ausente, em outro formato ou
    nem sequer ser texto. Nenhum desses casos pode derrubar o relatório do dia,
    então todos viram `None`.
    """
    if not isinstance(campos_extraidos, dict):
        return None
    valor: Any = campos_extraidos.get("data_atendimento")
    try:
        return date.fromisoformat(valor)
    except (TypeError, ValueError):
        return None


def _condicoes(filtro: FiltroConferencia) -> list[ColumnElement[bool]]:
    condicoes: list[ColumnElement[bool]] = []
    if filtro.competencia is not None:
        condicoes.append(Documento.competencia == filtro.competencia)
    if filtro.status is not None:
        condicoes.append(Documento.status == filtro.status)
    if filtro.operadora_id is not None:
        condicoes.append(Documento.operadora_id == filtro.operadora_id)
    if filtro.paciente_id is not None:
        condicoes.append(Documento.paciente_id == filtro.paciente_id)
    if filtro.data_inicio is not None:
        condicoes.append(
            Documento.created_at >= datetime.combine(filtro.data_inicio, time.min, tzinfo=UTC)
        )
    if filtro.data_fim is not None:
        # Fim do dia, e não meia-noite: `data_fim` é inclusive, igual ao que
        # `pendencias.listar_pendencias` já faz com o filtro de deadline.
        condicoes.append(
            Documento.created_at <= datetime.combine(filtro.data_fim, time.max, tzinfo=UTC)
        )
    if filtro.apenas_pendentes:
        condicoes.append(
            exists().where(
                Pendencia.documento_id == Documento.id,
                Pendencia.status != PendenciaStatus.RESOLVIDA,
            )
        )
    return condicoes


def contar_documentos(session: Session, filtro: FiltroConferencia) -> int:
    """Total de documentos do filtro, para o envelope de paginação."""
    return session.execute(
        select(func.count()).select_from(Documento).where(*_condicoes(filtro))
    ).scalar_one()


def montar_linhas(
    session: Session, filtro: FiltroConferencia, *, limite: int, offset: int
) -> list[LinhaConferencia]:
    """Monta uma página do relatório em quatro consultas, nunca uma por documento."""
    cabecalhos = session.execute(
        select(Documento, Paciente.nome, Operadora.nome)
        .outerjoin(Paciente, Paciente.id == Documento.paciente_id)
        .outerjoin(Operadora, Operadora.id == Documento.operadora_id)
        .where(*_condicoes(filtro))
        .order_by(
            _ORDEM_POR_PRIORIDADE,
            # Sem deadline aberto o subquery é `NULL`, e o Postgres ordena
            # `NULL` por último em ordem crescente — que é onde esse documento
            # deve ficar dentro da sua faixa de prioridade.
            _MENOR_DEADLINE_ABERTO.asc(),
            Documento.created_at.desc(),
            # Desempate final e estável: sem ele duas páginas do CSV poderiam
            # repetir ou pular um documento com os mesmos status/deadline/hora.
            Documento.id,
        )
        .limit(limite)
        .offset(offset)
    ).all()
    if not cabecalhos:
        return []

    ids = [documento.id for documento, _, _ in cabecalhos]
    pendencias_por_documento = _pendencias_abertas_por_documento(session, ids)
    data_por_documento = _data_atendimento_por_documento(session, ids)

    linhas: list[LinhaConferencia] = []
    for documento, paciente_nome, operadora_nome in cabecalhos:
        pendencias = pendencias_por_documento.get(documento.id, [])
        deadline = min((p.deadline for p in pendencias), default=None)
        linhas.append(
            LinhaConferencia(
                documento_id=documento.id,
                tipo=documento.tipo,
                competencia=documento.competencia,
                status=documento.status,
                severidade=severidade_de(documento.status),
                recebido_em=documento.created_at,
                data_atendimento=data_por_documento.get(documento.id),
                paciente_id=documento.paciente_id,
                paciente_nome=paciente_nome,
                operadora_id=documento.operadora_id,
                operadora_nome=operadora_nome,
                pendencias_abertas=len(pendencias),
                problema_encontrado=" | ".join(p.descricao for p in pendencias),
                acao_necessaria=acao_necessaria(documento.status, len(pendencias), deadline),
                deadline=deadline,
            )
        )
    return linhas


def iterar_paginas(
    session: Session,
    filtro: FiltroConferencia,
    *,
    tamanho: int = TAMANHO_PAGINA_EXPORTACAO,
) -> Iterator[list[LinhaConferencia]]:
    """Percorre o extrato inteiro do filtro em páginas, para o exportador de CSV."""
    offset = 0
    while True:
        pagina = montar_linhas(session, filtro, limite=tamanho, offset=offset)
        if not pagina:
            return
        yield pagina
        if len(pagina) < tamanho:
            return
        offset += tamanho


def _pendencias_abertas_por_documento(
    session: Session, ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[Pendencia]]:
    """Uma consulta para a página inteira, agrupada em Python por documento."""
    agrupadas: dict[uuid.UUID, list[Pendencia]] = defaultdict(list)
    pendencias = session.scalars(
        select(Pendencia)
        .where(
            Pendencia.documento_id.in_(ids),
            Pendencia.status != PendenciaStatus.RESOLVIDA,
        )
        .order_by(Pendencia.created_at)
    )
    for pendencia in pendencias:
        agrupadas[pendencia.documento_id].append(pendencia)
    return agrupadas


def _data_atendimento_por_documento(
    session: Session, ids: list[uuid.UUID]
) -> dict[uuid.UUID, date | None]:
    """Data de atendimento da **última** extração de cada documento da página.

    Uma consulta só, ordenada por documento e data decrescente: a primeira linha
    de cada documento é a extração mais recente, que é a que reflete a última
    correção enviada.
    """
    por_documento: dict[uuid.UUID, date | None] = {}
    linhas = session.execute(
        select(Extracao.documento_id, Extracao.campos_extraidos)
        .where(Extracao.documento_id.in_(ids))
        .order_by(Extracao.documento_id, Extracao.created_at.desc())
    )
    for documento_id, campos in linhas:
        if documento_id not in por_documento:
            por_documento[documento_id] = data_atendimento_de(campos)
    return por_documento
