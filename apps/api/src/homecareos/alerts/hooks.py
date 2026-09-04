"""Gancho de alerta chamado pela classificação — o único ponto de acoplamento.

A dependência é de mão única: `alerts` conhece `classification` (lê pendência e
documento), `classification` só conhece esta função. Não há ciclo de import.
"""

from __future__ import annotations

import logging
import uuid

from homecareos.alerts.canais import (
    alguma_credencial_presente,
    canais_que_enviam,
    construir_canais,
)
from homecareos.alerts.detectores import detectar_documento_incompleto_critico
from homecareos.alerts.service import despachar
from homecareos.config import get_settings
from homecareos.db.session import get_sessionmaker

logger = logging.getLogger(__name__)


def notificar_classificacao(documento_id: uuid.UUID) -> None:
    """Dispara o alerta de documento incompleto crítico logo após a classificação.

    Best-effort e blindado: **esta função NUNCA levanta.** Notificação não pode
    derrubar ingestão de documento — o produto do sistema é a conferência, e o
    aviso é um acessório dela. Gateway fora do ar, token vencido, SMTP
    recusando, JSON de destinatário malformado: tudo vira `logger.exception` e
    a classificação segue commitada como se o alerta não existisse.

    Três decisões que valem registro:

    - Sai **antes de abrir sessão** quando o gancho está desligado ou quando
      **nenhum canal tem credencial**. Abrir conexão para descobrir que não há
      o que fazer é custo no caminho do upload. Desde o ADR 0006 (parte 2) o
      liga/desliga vem da tabela `canais_alerta`, e descobrir que todo canal
      está desligado passou a exigir a sessão — mas a metade barata da guarda
      continua valendo, e ela é a que cobre o ambiente sem gateway nenhum
      configurado.
    - Abre a **própria** sessão, nunca a do chamador. A transação da
      classificação já commitou e é dele; um erro de escrita do log de alerta
      não pode desfazer uma classificação que deu certo.
    - Roda **só** o detector de documento incompleto crítico, restrito a este
      documento. Os outros três (deadline de competência, volume anormal,
      pendência parada) são de varredura, não de evento: só fazem sentido
      olhando a base inteira, e nenhum deles ficou mais verdadeiro por causa
      deste upload.
    """
    try:
        settings = get_settings()
        if not settings.alertas_hook_inline_habilitado:
            return

        if not alguma_credencial_presente(settings):
            # Sem uazapi e sem SMTP nenhum estado do banco faz um canal enviar:
            # a resposta já é "não há o que fazer", e ela sai sem conexão.
            return

        with get_sessionmaker()() as session:
            canais = construir_canais(session, settings)
            if not canais_que_enviam(canais):
                return
            alertas = detectar_documento_incompleto_critico(
                session, settings, documento_id=documento_id
            )
            if not alertas:
                return
            despachar(session, settings, canais, alertas)
    except Exception:
        logger.exception(
            "gancho de alerta falhou para o documento %s; a classificação não é afetada",
            documento_id,
        )
