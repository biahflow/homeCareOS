"""Acesso a `canais_alerta` e `auditoria_canais_alerta` — ADR 0006, parte 2 (issue #9).

Este módulo é a **fonte do liga/desliga** dos canais depois do ADR 0006. Antes
dele a resposta vinha de `ALERTAS_CANAIS` (`alerts/config.canais_habilitados`),
e mudá-la exigia acesso ao servidor; agora vem do banco, e quem a muda é o
coordenador, pela API.

## As duas tabelas moram juntas de propósito

`alerts/repository.py` é o acesso a `alertas_enviados` e diz isso na primeira
linha; misturar outra tabela lá contradiria a própria docstring. Aqui as duas
tabelas são a mesma entidade de domínio — o canal e a história dele — e
`definir_habilitado` precisa escrever nas duas **na mesma transação**: uma
mudança de estado sem o registro correspondente é exatamente o que o ADR proíbe,
porque quem desliga um canal silencia a operação.

## Nada aqui commita

Mesma regra de `alerts/repository.py`, `auth.auditoria.registrar` e
`auth.sessoes.revogar_todas`: quem decide o limite da transação é quem chama.
A exceção é `limpar_auditoria_canais_antiga`, que commita por lote — como as
outras funções de expurgo por idade, e pelo mesmo motivo (ver
`auth/protecao.limpar_tentativas_antigas`).

## Canal do enum sem linha na tabela conta como DESLIGADO

A migration semeia uma linha por canal e o `seed.py` cobre o canal que nascer
depois dela, então a ausência é anomalia — mas o comportamento precisa estar
definido, e derrubar a varredura seria trocar "um canal não envia" por "ninguém
é avisado de nada". Ausente é desligado, com `logger.warning`: é raro o
bastante para o ruído valer o aviso.

Linha com um `canal` que o enum não conhece é ignorada pela razão simétrica: o
código é a autoridade sobre quais canais existem, e uma linha órfã de um canal
retirado não pode impedir os outros de enviar.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from homecareos.alerts.schema import Canal
from homecareos.db.models import AuditoriaCanal, ConfiguracaoCanal

logger = logging.getLogger(__name__)


def listar_estado(session: Session) -> dict[Canal, ConfiguracaoCanal]:
    """As linhas de `canais_alerta`, por canal conhecido. Ignora canal desconhecido."""
    estado: dict[Canal, ConfiguracaoCanal] = {}
    for linha in session.scalars(select(ConfiguracaoCanal).order_by(ConfiguracaoCanal.canal)).all():
        try:
            estado[Canal(linha.canal)] = linha
        except ValueError:
            # Canal que o código não conhece mais (retirado do enum). Não é erro
            # desta leitura: ele simplesmente não é construído nem enviado.
            logger.warning(
                "canais_alerta tem a linha do canal desconhecido %r; ignorada", linha.canal
            )
    return estado


def canais_habilitados(session: Session) -> set[Canal]:
    """Os canais ligados **no banco** — a resposta que `ALERTAS_CANAIS` dava antes.

    Habilitado não é o mesmo que envia: falta a credencial, que continua no
    `.env` (ver `alerts/canais.py`).
    """
    estado = listar_estado(session)
    ausentes = [canal for canal in Canal if canal not in estado]
    if ausentes:
        # Estado anômalo: a migration semeia todos e o seed cobre o canal novo.
        # Desligado é a leitura conservadora — ligar um canal que ninguém
        # configurou mandaria mensagem que ninguém pediu.
        logger.warning(
            "canais sem linha em canais_alerta, tratados como desligados: %s",
            ", ".join(canal.value for canal in ausentes),
        )
    return {canal for canal, linha in estado.items() if linha.habilitado}


def definir_habilitado(
    session: Session,
    *,
    canal: Canal,
    habilitado: bool,
    ator: str,
    ator_id: uuid.UUID | None,
    agora: datetime,
) -> bool:
    """Liga/desliga um canal e registra a mudança. **Não commita.** Devolve se mudou.

    A auditoria é escrita **aqui**, e não pelo chamador como em
    `auth/usuarios_router.py`: lá o router precisa interpretar um diff de vários
    campos para escolher o rótulo da ação, e a decisão é dele; aqui não há diff
    a interpretar, e acoplar o registro à escrita é o que impede a mudança
    silenciosa que o ADR 0006 proíbe. As duas linhas entram na mesma transação
    do chamador.

    **Uma mudança que não muda nada não é evento**: ligar um canal já ligado não
    gera registro e não mexe em `atualizado_em`/`atualizado_por` — esse par
    responde "quem decidiu o estado atual", não "quem clicou por último".

    Canal válido sem linha na tabela é **criado** aqui, e o estado anterior
    considerado `False`: é como `canais_habilitados` o lê, e responder 404 numa
    linha que sumiu deixaria o coordenador sem como consertar justamente o canal
    que não está enviando. Esse caminho é o único sem o lock abaixo — não há
    linha para travar —, e o `unique` de `canais_alerta.canal` é quem garante a
    integridade se duas criações correrem juntas; é anomalia sobre anomalia, e
    já vem precedida de `logger.warning` em `canais_habilitados`.
    """
    # `with_for_update`: dois coordenadores clicando no mesmo canal ao mesmo
    # tempo leriam o mesmo `anterior` e gravariam dois eventos com o mesmo
    # "de" — um deles falso. Numa tabela de duas linhas escritas por gesto
    # humano o lock não custa nada, e auditoria com valor anterior errado é
    # pior que auditoria nenhuma: ela responde com confiança a pergunta errada.
    # Mesmo uso de `auth/mfa.py` e `auth/recuperacao.py`.
    linha = session.scalars(
        select(ConfiguracaoCanal).where(ConfiguracaoCanal.canal == canal.value).with_for_update()
    ).one_or_none()

    if linha is None:
        anterior = False
        linha = ConfiguracaoCanal(canal=canal.value, habilitado=anterior)
        session.add(linha)
    else:
        anterior = linha.habilitado

    if anterior == habilitado:
        # Garante a linha (para o canal que não tinha nenhuma) sem inventar uma
        # decisão: nada mudou, então `atualizado_*` continua dizendo a verdade.
        session.flush()
        return False

    linha.habilitado = habilitado
    linha.atualizado_em = agora
    linha.atualizado_por = ator
    linha.atualizado_por_usuario_id = ator_id
    session.add(
        AuditoriaCanal(
            usuario=ator,
            usuario_id=ator_id,
            canal=canal.value,
            habilitado_de=anterior,
            habilitado_para=habilitado,
            created_at=agora,
        )
    )
    session.flush()
    return True


def listar_auditoria(
    session: Session,
    *,
    canal: Canal | None = None,
    ator_id: uuid.UUID | None = None,
    habilitado: bool | None = None,
    limite: int,
    offset: int,
) -> tuple[list[AuditoriaCanal], int]:
    """Página do histórico e o total do filtro, do mais recente para o mais antigo."""
    stmt = select(AuditoriaCanal)
    contagem = select(func.count()).select_from(AuditoriaCanal)
    if canal is not None:
        stmt = stmt.where(AuditoriaCanal.canal == canal.value)
        contagem = contagem.where(AuditoriaCanal.canal == canal.value)
    if ator_id is not None:
        stmt = stmt.where(AuditoriaCanal.usuario_id == ator_id)
        contagem = contagem.where(AuditoriaCanal.usuario_id == ator_id)
    if habilitado is not None:
        stmt = stmt.where(AuditoriaCanal.habilitado_para == habilitado)
        contagem = contagem.where(AuditoriaCanal.habilitado_para == habilitado)

    total = int(session.execute(contagem).scalar_one())
    linhas = list(
        session.scalars(
            # Desempate por `id`: `created_at` sozinho empata quando duas
            # mudanças acontecem no mesmo instante (dois canais no mesmo
            # script), e sem desempate a paginação por offset duplica ou pula
            # linha — mesma razão de `auth/auditoria_router.py`.
            stmt.order_by(AuditoriaCanal.created_at.desc(), AuditoriaCanal.id.desc())
            .limit(limite)
            .offset(offset)
        ).all()
    )
    return linhas, total


def limpar_auditoria_canais_antiga(
    session: Session, *, antes_de: datetime, lote: int = 1000, dry_run: bool = False
) -> int:
    """Apaga eventos com `created_at < antes_de` e devolve quantos saíram (ou sairiam).

    Commita a cada lote de até `lote` linhas — ver
    `auth/protecao.limpar_tentativas_antigas` para o motivo do lote/commit por
    lote e do default. É o **único** caminho de exclusão desta tabela: a API é
    append-only, sem `DELETE`.

    Sem exceção por linha, como em `auth.auditoria.limpar_auditoria_antiga`:
    não existe evento "ainda em uso" que a idade não capture. A proteção desta
    tabela é o piso de retenção (`retencao/janelas.MINIMO_AUDITORIA_CANAIS`),
    não uma cláusula no `WHERE`.
    """
    condicao = AuditoriaCanal.created_at < antes_de
    if dry_run:
        total = session.scalar(select(func.count()).select_from(AuditoriaCanal).where(condicao))
        return int(total or 0)

    total = 0
    while True:
        subquery = select(AuditoriaCanal.id).where(condicao).limit(lote)
        resultado = cast(
            "CursorResult[Any]",
            session.execute(delete(AuditoriaCanal).where(AuditoriaCanal.id.in_(subquery))),
        )
        session.commit()
        apagadas = resultado.rowcount
        total += apagadas
        if apagadas < lote:
            break
    return total
