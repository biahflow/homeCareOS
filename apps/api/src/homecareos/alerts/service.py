"""Orquestração da varredura: detectar → filtrar → enviar → registrar.

## A política anti-bombardeio, que é o coração deste módulo

Um sistema de alerta falha de duas maneiras. A óbvia é não avisar. A que
acontece de verdade é avisar demais: a equipe cria o hábito de deslizar a
notificação sem ler, e a partir daí o alerta que importava também não é lido.
As duas defesas abaixo existem para isso, e elas são **diferentes de
propósito**:

- **Cooldown** (mesmo assunto, mesmo destinatário, dentro da janela): pula em
  **silêncio, sem gravar linha nenhuma**. A varredura roda de minuto em minuto
  no cron; gravar "suprimido por cooldown" a cada passada encheria
  `alertas_enviados` com centenas de linhas por dia por alerta, e o log de
  auditoria — que existe para alguém achar as falhas — viraria ruído que as
  esconde. Não gravar é o que mantém a tabela legível.
- **Rate limit** (teto de mensagens por destinatário por hora): **grava** linha
  `suprimido`, com o motivo. Esta supressão é anômala: significa que houve
  alerta demais para uma pessoa só e que alguma notificação real foi perdida.
  Alguém precisa poder descobrir isso depois, e a única forma é ter a linha.

Ambas contam em `resumo.suprimidos` — do ponto de vista de quem lê o resumo, os
dois casos são "detectei e não mandei".
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from homecareos.alerts import config, repository, templates
from homecareos.alerts.detectores import detectar_todos
from homecareos.alerts.errors import EnvioError
from homecareos.alerts.provider import WhatsAppProvider
from homecareos.alerts.schema import Alerta, ResumoVarredura, StatusAlerta, TipoAlerta
from homecareos.config import Settings

logger = logging.getLogger(__name__)

JANELA_RATE_LIMIT = timedelta(hours=1)


def executar_varredura(
    session: Session, settings: Settings, provider: WhatsAppProvider | None
) -> ResumoVarredura:
    """Roda os quatro detectores e despacha o que eles acharem."""
    return despachar(session, settings, provider, detectar_todos(session, settings))


def despachar(
    session: Session,
    settings: Settings,
    provider: WhatsAppProvider | None,
    alertas: Sequence[Alerta],
) -> ResumoVarredura:
    """Aplica a política anti-bombardeio, envia o que sobrou e registra tudo. Commita."""
    resumo = ResumoVarredura(
        detectados=len(alertas),
        por_tipo=_contar_por_tipo(alertas),
        provider_configurado=provider is not None,
    )

    # Antes da guarda de provider ausente: um typo em ALERTAS_DESTINATARIOS
    # precisa aparecer como 422 mesmo em ambiente sem gateway configurado —
    # senão só se descobre a configuração quebrada no dia em que o alerta
    # deveria ter saído.
    destinatarios_por_tipo = config.destinatarios(settings)

    if provider is None:
        # Nada é enviado e nada é gravado: uma linha `falha` aqui registraria
        # como problema do gateway o que é uma decisão de configuração.
        return resumo

    # `agora` é calculado UMA vez e passado adiante. Chamar `now()` dentro do
    # laço faria a janela do rate limit deslizar entre um destinatário e o
    # seguinte — diferença irrelevante em produção, mas suficiente para tornar
    # o teste de borda do limite dependente de corrida.
    agora = datetime.now(UTC)
    inicio_cooldown = agora - timedelta(hours=settings.alertas_cooldown_horas)
    inicio_janela_rate_limit = agora - JANELA_RATE_LIMIT

    for alerta in alertas:
        destinatarios = destinatarios_por_tipo.get(alerta.tipo, [])
        if not destinatarios:
            # Tipo sem destinatário é o jeito de desligar um alerta; não é falha
            # nem supressão, e não conta em lugar nenhum do resumo.
            continue

        mensagem = templates.renderizar(alerta.tipo, alerta.contexto, settings)
        for destinatario in destinatarios:
            _despachar_para(
                session,
                settings,
                provider,
                alerta,
                mensagem=mensagem,
                destinatario=destinatario,
                inicio_cooldown=inicio_cooldown,
                inicio_janela_rate_limit=inicio_janela_rate_limit,
                resumo=resumo,
            )

    session.commit()
    return resumo


def _despachar_para(
    session: Session,
    settings: Settings,
    provider: WhatsAppProvider,
    alerta: Alerta,
    *,
    mensagem: str,
    destinatario: str,
    inicio_cooldown: datetime,
    inicio_janela_rate_limit: datetime,
    resumo: ResumoVarredura,
) -> None:
    """Um alerta, um destinatário: cooldown, rate limit, envio e registro."""
    if repository.existe_envio_recente(
        session,
        tipo=alerta.tipo,
        chave=alerta.chave,
        destinatario=destinatario,
        desde=inicio_cooldown,
    ):
        # Supressão silenciosa, sem linha no banco — ver a docstring do módulo.
        resumo.suprimidos += 1
        return

    enviados_na_hora = repository.contar_envios_desde(
        session, destinatario=destinatario, desde=inicio_janela_rate_limit
    )
    limite = settings.alertas_max_por_hora_por_destinatario
    if enviados_na_hora >= limite:
        repository.registrar(
            session,
            tipo=alerta.tipo,
            chave=alerta.chave,
            destinatario=destinatario,
            mensagem=mensagem,
            status=StatusAlerta.SUPRIMIDO,
            detalhe=f"rate limit: {enviados_na_hora} envios na última hora (limite {limite})",
            documento_id=alerta.documento_id,
        )
        resumo.suprimidos += 1
        return

    try:
        provider.enviar(destinatario, mensagem)
    except EnvioError as exc:
        # A falha de um destinatário não pode cancelar os outros: os alertas
        # restantes são de outras pessoas e de outros problemas.
        logger.warning("falha ao enviar alerta %s para o gateway: %s", alerta.tipo.value, exc)
        repository.registrar(
            session,
            tipo=alerta.tipo,
            chave=alerta.chave,
            destinatario=destinatario,
            mensagem=mensagem,
            status=StatusAlerta.FALHA,
            detalhe=str(exc),
            documento_id=alerta.documento_id,
        )
        resumo.falhas += 1
        return

    repository.registrar(
        session,
        tipo=alerta.tipo,
        chave=alerta.chave,
        destinatario=destinatario,
        mensagem=mensagem,
        status=StatusAlerta.ENVIADO,
        documento_id=alerta.documento_id,
    )
    resumo.enviados += 1


def _contar_por_tipo(alertas: Sequence[Alerta]) -> dict[str, int]:
    """Contagem por tipo, sempre com os quatro tipos (ver `ResumoVarredura`)."""
    contagem = Counter(alerta.tipo.value for alerta in alertas)
    return {tipo.value: contagem.get(tipo.value, 0) for tipo in TipoAlerta}
