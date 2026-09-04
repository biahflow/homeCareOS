"""Acesso a `alertas_enviados`: grava o log e responde as perguntas do anti-bombardeio.

Nenhuma função aqui commita. Quem decide o limite da transação é
`alerts/service.py`, que commita uma vez ao final da varredura — as linhas de
uma passada entram juntas ou não entram.

`registrar` faz `flush()` de propósito. O `sessionmaker` do projeto é
`autoflush=False` (ver `db/session.py`), então uma linha só `add`-ada seria
invisível para as consultas de cooldown e rate limit da MESMA varredura: com
`max_por_hora=1`, dois alertas para o mesmo destinatário na mesma passada
seriam ambos enviados, porque o segundo não enxergaria o primeiro. O `flush`
escreve dentro da transação (não commita) e é o que fecha esse buraco.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from homecareos.alerts.schema import StatusAlerta, TipoAlerta
from homecareos.db.models import AlertaEnviado


def registrar(
    session: Session,
    *,
    tipo: TipoAlerta,
    chave: str,
    destinatario: str,
    mensagem: str,
    status: StatusAlerta,
    detalhe: str | None = None,
    documento_id: uuid.UUID | None = None,
) -> AlertaEnviado:
    """Enfileira a linha de auditoria e a torna visível para a própria varredura."""
    linha = AlertaEnviado(
        tipo=tipo.value,
        chave=chave,
        destinatario=destinatario,
        mensagem=mensagem,
        status=status.value,
        detalhe=detalhe,
        documento_id=documento_id,
    )
    session.add(linha)
    session.flush()
    return linha


def existe_envio_recente(
    session: Session, *, tipo: TipoAlerta, chave: str, destinatario: str, desde: datetime
) -> bool:
    """Este destinatário já foi avisado deste mesmo assunto desde `desde`?

    Só conta linha `enviado`: uma falha ou uma supressão anterior não é aviso
    entregue, e tratá-la como tal deixaria o destinatário sem nunca saber do
    problema.
    """
    return (
        session.execute(
            select(AlertaEnviado.id)
            .where(
                AlertaEnviado.tipo == tipo.value,
                AlertaEnviado.chave == chave,
                AlertaEnviado.destinatario == destinatario,
                AlertaEnviado.status == StatusAlerta.ENVIADO.value,
                AlertaEnviado.created_at >= desde,
            )
            .limit(1)
        ).first()
        is not None
    )


def contar_envios_desde(session: Session, *, destinatario: str, desde: datetime) -> int:
    """Quantas mensagens este número recebeu de fato desde `desde` (só `enviado`)."""
    return int(
        session.execute(
            select(func.count())
            .select_from(AlertaEnviado)
            .where(
                AlertaEnviado.destinatario == destinatario,
                AlertaEnviado.status == StatusAlerta.ENVIADO.value,
                AlertaEnviado.created_at >= desde,
            )
        ).scalar_one()
    )


def listar(
    session: Session,
    *,
    tipo: TipoAlerta | None = None,
    status: StatusAlerta | None = None,
    documento_id: uuid.UUID | None = None,
    limite: int,
    offset: int,
) -> tuple[list[AlertaEnviado], int]:
    """Página do log e o total do filtro, do mais recente para o mais antigo."""
    stmt = select(AlertaEnviado)
    contagem = select(func.count()).select_from(AlertaEnviado)
    if tipo is not None:
        stmt = stmt.where(AlertaEnviado.tipo == tipo.value)
        contagem = contagem.where(AlertaEnviado.tipo == tipo.value)
    if status is not None:
        stmt = stmt.where(AlertaEnviado.status == status.value)
        contagem = contagem.where(AlertaEnviado.status == status.value)
    if documento_id is not None:
        stmt = stmt.where(AlertaEnviado.documento_id == documento_id)
        contagem = contagem.where(AlertaEnviado.documento_id == documento_id)

    total = int(session.execute(contagem).scalar_one())
    linhas = list(
        session.scalars(
            stmt.order_by(AlertaEnviado.created_at.desc()).limit(limite).offset(offset)
        ).all()
    )
    return linhas, total


def limpar_alertas_antigos(
    session: Session, *, antes_de: datetime, lote: int = 1000, dry_run: bool = False
) -> int:
    """Apaga linhas de `alertas_enviados` com `created_at < antes_de` e devolve
    quantas saíram (ou sairiam, em `dry_run`). Commita a cada lote de até
    `lote` linhas — ver `auth/protecao.limpar_tentativas_antigas` para o
    motivo do lote/commit por lote e do default de `lote`. Ver
    `retencao/cli.py` (issue #39).

    `mensagem` guarda o texto enviado, incluindo o nome do paciente (ver
    `db/models/alerta.py`) — dado pessoal de saúde retido para sempre não é
    neutro, é exposição que só cresce.
    """
    condicao = AlertaEnviado.created_at < antes_de
    if dry_run:
        total = session.scalar(select(func.count()).select_from(AlertaEnviado).where(condicao))
        return int(total or 0)

    total = 0
    while True:
        subquery = select(AlertaEnviado.id).where(condicao).limit(lote)
        resultado = cast(
            "CursorResult[Any]",
            session.execute(delete(AlertaEnviado).where(AlertaEnviado.id.in_(subquery))),
        )
        session.commit()
        apagadas = resultado.rowcount
        total += apagadas
        if apagadas < lote:
            break
    return total
