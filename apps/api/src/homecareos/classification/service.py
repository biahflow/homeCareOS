"""Lado com I/O da classificação: transições de status, pendências e revalidação.

Fecha o ciclo que a issue #7 pede:

    extração -> regras -> classificação -> { aprovado | problema | incompleto }
                              equipe transiciona as pendências
    todas resolvidas -> revalidação -> { liberado | bucket reaberto }

Este é o único módulo que escreve `documentos.status` fora do intake, e toda
escrita passa por `transicionar()` — que é o que garante a linha em
`log_conferencia` exigida pelo critério de aceite de auditoria.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, time

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from homecareos.classification.engine import calcular_deadline, classificar
from homecareos.classification.errors import (
    DocumentoNaoEncontradoError,
    RevalidacaoIndisponivelError,
    TransicaoInvalidaError,
)
from homecareos.classification.schema import PendenciaProposta
from homecareos.config import get_settings
from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    Extracao,
    LogConferencia,
    Operadora,
    Pendencia,
    PendenciaStatus,
)
from homecareos.extraction.schema import EvolucaoProntuario
from homecareos.rules.engine import validar
from homecareos.rules.repository import buscar_regras_ativas, registrar_validacoes
from homecareos.rules.schema import ResultadoAvaliacao

# Máquina de estados do documento. O resumo legível vive na docstring de
# `db.models.enums.DocumentoStatus`; a autoridade é este mapa.
#
# Duas entradas são extensão deliberada do ciclo original desenhado lá:
#
# 1. `RESOLVIDO -> PROBLEMA|INCOMPLETO`: a revalidação pode reprovar de novo.
#    Sem isso o ciclo morre num beco — um documento marcado "resolvido" que
#    continua irregular nunca mais seria olhado, e ele é exatamente o caso que
#    vira glosa.
# 2. A auto-transição `PROBLEMA -> PROBLEMA` (e `INCOMPLETO -> INCOMPLETO`):
#    revalidar e reconfirmar o mesmo bucket é um evento real da conferência e
#    merece a sua linha em `log_conferencia`; tratá-lo como transição inválida
#    apagaria do histórico a tentativa de correção que não resolveu nada.
# 3. `EM_CORRECAO -> LIBERADO`, sem passar por `RESOLVIDO`: revalidar um
#    documento em correção que agora passa nas regras é resultado legítimo — e
#    `_reconciliar_pendencias` já fecha as pendências que deixaram de reprovar,
#    então não sobra correção pendente que justificasse recusar a transição.
_TRANSICOES_VALIDAS: dict[DocumentoStatus, frozenset[DocumentoStatus]] = {
    DocumentoStatus.PROCESSANDO: frozenset(
        {DocumentoStatus.APROVADO, DocumentoStatus.PROBLEMA, DocumentoStatus.INCOMPLETO}
    ),
    DocumentoStatus.PROBLEMA: frozenset(
        {
            DocumentoStatus.EM_CORRECAO,
            DocumentoStatus.PROBLEMA,
            DocumentoStatus.INCOMPLETO,
            DocumentoStatus.LIBERADO,
        }
    ),
    DocumentoStatus.INCOMPLETO: frozenset(
        {
            DocumentoStatus.EM_CORRECAO,
            DocumentoStatus.PROBLEMA,
            DocumentoStatus.INCOMPLETO,
            DocumentoStatus.LIBERADO,
        }
    ),
    DocumentoStatus.EM_CORRECAO: frozenset(
        {
            DocumentoStatus.RESOLVIDO,
            DocumentoStatus.PROBLEMA,
            DocumentoStatus.INCOMPLETO,
            DocumentoStatus.LIBERADO,
        }
    ),
    DocumentoStatus.RESOLVIDO: frozenset(
        {DocumentoStatus.LIBERADO, DocumentoStatus.PROBLEMA, DocumentoStatus.INCOMPLETO}
    ),
    # Terminais: documento aprovado direto ou liberado depois da correção já
    # seguiu para o faturamento; reabri-lo seria mexer em lote já enviado.
    DocumentoStatus.APROVADO: frozenset(),
    DocumentoStatus.LIBERADO: frozenset(),
}


def transicionar(
    session: Session,
    documento: Documento,
    novo: DocumentoStatus,
    *,
    usuario: str,
    detalhe: str,
) -> None:
    """Transiciona o status e registra a transição em `log_conferencia`. Não commita.

    Não commita de propósito: quem chama decide o limite da transação — a
    transição do documento e as pendências que a acompanham precisam entrar
    juntas.
    """
    anterior = documento.status
    if novo not in _TRANSICOES_VALIDAS.get(anterior, frozenset()):
        raise TransicaoInvalidaError(
            f"transição de {anterior.value!r} para {novo.value!r} não é permitida"
        )
    documento.status = novo
    registrar_log(
        session,
        documento_id=documento.id,
        acao=f"transicao:{anterior.value}->{novo.value}",
        usuario=usuario,
        detalhe=detalhe,
    )


def registrar_log(
    session: Session, *, documento_id: uuid.UUID, acao: str, usuario: str, detalhe: str
) -> None:
    """Enfileira uma linha de auditoria na sessão. Não commita.

    Mesmo formato de `intake.repository.DocumentoRepository.registrar_log`, sem
    a dependência daquela classe (o intake é dono de `documentos`, não do log).
    """
    session.add(
        LogConferencia(
            documento_id=documento_id,
            acao=acao,
            usuario=usuario,
            detalhe=detalhe,
        )
    )


def classificar_documento(
    session: Session,
    documento_id: uuid.UUID,
    resultados: Sequence[ResultadoAvaliacao],
    *,
    usuario: str,
) -> DocumentoStatus:
    """Classifica, transiciona o documento e abre as pendências. Commita."""
    documento = session.get(Documento, documento_id)
    if documento is None:
        raise DocumentoNaoEncontradoError(f"documento {documento_id} não encontrado")

    classificacao = classificar(resultados)
    alvo = _status_alvo(documento.status, classificacao.status)
    transicionar(
        session,
        documento,
        alvo,
        usuario=usuario,
        detalhe=(
            f"classificação automática: {len(classificacao.pendencias)} pendência(s) "
            f"a partir de {len(resultados)} regra(s) avaliada(s)"
        ),
    )

    a_criar = _reconciliar_pendencias(session, documento, classificacao.pendencias, usuario=usuario)
    if a_criar:
        deadline = _deadline_das_pendencias(session, documento, usuario=usuario)
        responsavel = get_settings().pendencia_responsavel_padrao
        for proposta in a_criar:
            session.add(_pendencia(documento_id, proposta, deadline, responsavel))

    session.commit()
    return alvo


def _chave(campo: str | None, tipo_problema: str) -> tuple[str | None, str]:
    """Identidade de um problema: o campo violado e a natureza da violação.

    Não entra a `descricao`: ela carrega o detalhe da avaliação e o motivo de
    glosa, que mudam quando a regra é editada sem que o problema tenha mudado.
    """
    return (campo, tipo_problema)


def _reconciliar_pendencias(
    session: Session,
    documento: Documento,
    propostas: Sequence[PendenciaProposta],
    *,
    usuario: str,
) -> list[PendenciaProposta]:
    """Casa as pendências já abertas com o que a classificação acabou de propor.

    `POST /api/documentos/{id}/revalidar` é público e chamável a qualquer
    momento; sem esta reconciliação, cada chamada duplicaria a pendência que
    continua reprovando e deixaria órfã a que parou de reprovar — no pior caso
    um documento `liberado` carregando pendência `aberta` para sempre.

    Devolve só as propostas que ainda não têm pendência aberta equivalente. A
    que sobrevive mantém `id`, `deadline` e `responsavel`: é o mesmo problema,
    com o mesmo prazo e a mesma pessoa — recriá-la reiniciaria o relógio de uma
    cobrança que já estava correndo.
    """
    abertas = list(
        session.scalars(
            select(Pendencia).where(
                Pendencia.documento_id == documento.id,
                Pendencia.status != PendenciaStatus.RESOLVIDA,
            )
        )
    )
    chaves_propostas = {_chave(p.campo, p.tipo_problema.value) for p in propostas}
    agora = datetime.now(UTC)
    sobreviventes: set[tuple[str | None, str]] = set()

    for pendencia in abertas:
        # Pendência com `campo` nulo (anterior à issue #7) nunca casa com
        # proposta nenhuma — `campo` é obrigatório em toda proposta — e por isso
        # cai sempre aqui. É o comportamento desejado: não dá para afirmar que
        # ela ainda descreve um problema vivo, e mantê-la aberta para sempre é
        # o que a reconciliação existe para evitar.
        chave = _chave(pendencia.campo, pendencia.tipo_problema)
        if chave in chaves_propostas:
            sobreviventes.add(chave)
            continue
        pendencia.status = PendenciaStatus.RESOLVIDA
        pendencia.resolved_at = agora
        registrar_log(
            session,
            documento_id=documento.id,
            acao="pendencia:resolvida_por_revalidacao",
            usuario=usuario,
            detalhe=(
                f"pendência {pendencia.id} resolvida: o problema deixou de ser "
                f"apontado pelas regras ativas ({pendencia.descricao})"
            ),
        )

    return [p for p in propostas if _chave(p.campo, p.tipo_problema.value) not in sobreviventes]


def _pendencia(
    documento_id: uuid.UUID, proposta: PendenciaProposta, deadline: datetime, responsavel: str
) -> Pendencia:
    return Pendencia(
        documento_id=documento_id,
        campo=proposta.campo,
        tipo_problema=proposta.tipo_problema.value,
        descricao=proposta.descricao,
        responsavel=responsavel,
        status=PendenciaStatus.ABERTA,
        deadline=deadline,
    )


def _status_alvo(atual: DocumentoStatus, bucket: DocumentoStatus) -> DocumentoStatus:
    """Traduz o bucket da classificação para o status alvo, dado onde o documento está.

    `aprovado` é o bucket de quem passou nas regras na primeira conferência.
    Um documento que já abriu pendência, foi corrigido e agora passa não volta
    a ser "aprovado direto": ele é `liberado` — o estado que diz "houve
    conferência com correção e o resultado está bom".
    """
    if bucket is DocumentoStatus.APROVADO and atual is not DocumentoStatus.PROCESSANDO:
        return DocumentoStatus.LIBERADO
    return bucket


def _deadline_das_pendencias(session: Session, documento: Documento, *, usuario: str) -> datetime:
    """Deadline das pendências deste documento, com fallback registrado em log.

    `documentos.competencia` é `String` livre, sem constraint de formato, e a
    operadora pode não estar associada — nenhum dos dois pode derrubar um
    upload. Uma pendência sem prazo é pior que uma com prazo apertado: some do
    painel de vencimento e ninguém a cobra. No fallback o prazo é o fim de
    hoje, e o motivo fica em `log_conferencia` para alguém corrigir a origem.
    """
    motivo: str | None = None
    operadora = (
        session.get(Operadora, documento.operadora_id)
        if documento.operadora_id is not None
        else None
    )
    if operadora is None:
        motivo = "documento sem operadora associada"
    else:
        try:
            return calcular_deadline(documento.competencia, operadora.dia_envio)
        except ValueError as exc:
            motivo = str(exc)

    registrar_log(
        session,
        documento_id=documento.id,
        acao="deadline:fallback",
        usuario=usuario,
        detalhe=f"deadline calculado como fim do dia de hoje: {motivo}",
    )
    return datetime.combine(datetime.now(UTC).date(), time(23, 59, 59), tzinfo=UTC)


def revalidar_documento(
    session: Session, documento_id: uuid.UUID, *, usuario: str
) -> DocumentoStatus:
    """Reavalia a última extração contra as regras ativas e reclassifica. Commita.

    Não chama o provider de Vision de novo: revalidar é reaplicar as regras
    sobre o que já foi extraído — a extração custa dinheiro e o documento não
    mudou, só o que se sabe sobre ele.
    """
    documento = session.get(Documento, documento_id)
    if documento is None:
        raise DocumentoNaoEncontradoError(f"documento {documento_id} não encontrado")
    if documento.operadora_id is None:
        raise RevalidacaoIndisponivelError("documento sem operadora")

    extracao = session.scalars(
        select(Extracao)
        .where(Extracao.documento_id == documento_id)
        .order_by(Extracao.created_at.desc())
    ).first()
    if extracao is None:
        raise RevalidacaoIndisponivelError("documento sem extração")
    if not _TRANSICOES_VALIDAS.get(documento.status, frozenset()):
        # Antes de avaliar qualquer regra, e não no `transicionar()` lá embaixo:
        # `registrar_validacoes` commita sozinho, então descobrir o estado
        # terminal só na transição deixaria linhas novas em `validacoes` a cada
        # requisição recusada com 409.
        raise TransicaoInvalidaError(
            f"documento em {documento.status.value!r} é terminal: não há revalidação possível"
        )

    try:
        campos = EvolucaoProntuario.model_validate(extracao.campos_extraidos)
    except ValidationError as exc:
        raise RevalidacaoIndisponivelError(f"extração ilegível para revalidação: {exc}") from exc

    regras = buscar_regras_ativas(session, documento.operadora_id)
    if not regras:
        raise RevalidacaoIndisponivelError("operadora sem regras ativas")

    resultados = validar(campos, regras, competencia=documento.competencia)
    registrar_validacoes(session, documento_id, resultados)
    return classificar_documento(session, documento_id, resultados, usuario=usuario)
