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
- **Rate limit** (teto de mensagens por **pessoa** por hora): **grava** linha
  `suprimido`, com o motivo. Esta supressão é anômala: significa que houve
  alerta demais para uma pessoa só e que alguma notificação real foi perdida.
  Alguém precisa poder descobrir isso depois, e a única forma é ter a linha.

Ambas contam em `resumo.suprimidos` — do ponto de vista de quem lê o resumo, os
dois casos são "detectei e não mandei".

## Por que as duas contam sobre chaves diferentes (ADR 0006)

Com dois canais, o cooldown continua por **destinatário**: dois canais são dois
endereços, e o mesmo aviso sair no WhatsApp e no e-mail é o comportamento
desejado — canais independentes, nunca fallback um do outro.

O rate limit **não** pode seguir por endereço. Se seguisse, ligar o segundo
canal dobraria o teto por hora de quem recebe nos dois, sem ninguém pedir — o
teto existe para proteger a **pessoa**, não o endereço. Por isso ele conta por
`usuario_id` quando o sistema sabe de quem é o endereço, e cai para o endereço
quando não sabe (telefone avulso do `.env`, que é o melhor que o dado permite:
não há telefone em `usuarios`).

## O serviço não conhece canal nenhum em particular

Ele itera `CanalAlerta` (ver `alerts/canais.py`). Quem sabe o que é um
telefone, o que é um assunto de e-mail e como cada gateway recusa é o canal.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from homecareos.alerts import config, repository
from homecareos.alerts.canais import CanalAlerta, canais_que_enviam
from homecareos.alerts.detectores import detectar_todos
from homecareos.alerts.errors import EnvioError
from homecareos.alerts.schema import (
    Alerta,
    Canal,
    Destinatario,
    EstadoCanal,
    MensagemAlerta,
    ResumoVarredura,
    StatusAlerta,
    TipoAlerta,
)
from homecareos.config import Settings

logger = logging.getLogger(__name__)

JANELA_RATE_LIMIT = timedelta(hours=1)


def executar_varredura(
    session: Session, settings: Settings, canais: Sequence[CanalAlerta]
) -> ResumoVarredura:
    """Roda os quatro detectores e despacha o que eles acharem."""
    return despachar(session, settings, canais, detectar_todos(session, settings))


def despachar(
    session: Session,
    settings: Settings,
    canais: Sequence[CanalAlerta],
    alertas: Sequence[Alerta],
) -> ResumoVarredura:
    """Aplica a política anti-bombardeio, envia o que sobrou e registra tudo. Commita."""
    resumo = ResumoVarredura(
        detectados=len(alertas),
        por_tipo=_contar_por_tipo(alertas),
        canais=_estado_dos_canais(canais),
    )
    resumo.provider_configurado = _whatsapp_configurado(resumo)

    # Antes da guarda de canal indisponível: um typo em ALERTAS_DESTINATARIOS
    # (ou em ALERTAS_TEMPLATES, ou em ALERTAS_PAPEIS_EMAIL) precisa aparecer
    # como 422 mesmo em ambiente sem credencial nenhuma — senão só se descobre
    # a configuração quebrada no dia em que o alerta deveria ter saído.
    # ALERTAS_CANAIS saiu desta lista na parte 2 do ADR 0006: ela não decide
    # mais nada, e recusar a varredura por causa dela silenciaria a operação
    # por uma variável inerte (ver `alerts/config.validar`).
    config.validar(settings)

    ativos = canais_que_enviam(list(canais))
    if not ativos:
        # Nada é enviado e nada é gravado: uma linha `falha` aqui registraria
        # como problema do gateway o que é uma decisão de configuração (canal
        # desligado) ou uma configuração incompleta (sem credencial). O resumo
        # já distingue os dois casos em `canais`.
        return resumo

    # `agora` é calculado UMA vez e passado adiante. Chamar `now()` dentro do
    # laço faria a janela do rate limit deslizar entre um destinatário e o
    # seguinte — diferença irrelevante em produção, mas suficiente para tornar
    # o teste de borda do limite dependente de corrida.
    agora = datetime.now(UTC)
    inicio_cooldown = agora - timedelta(hours=settings.alertas_cooldown_horas)
    inicio_janela_rate_limit = agora - JANELA_RATE_LIMIT

    for alerta in alertas:
        for canal in ativos:
            destinatarios = canal.destinatarios(session, settings, alerta.tipo)
            if not destinatarios:
                # Tipo sem destinatário NESTE canal é o jeito de desligar um
                # alerta; não é falha nem supressão, e não conta em lugar
                # nenhum do resumo. Vale para o telefone ausente de
                # `ALERTAS_DESTINATARIOS` e para o papel que não tem nenhuma
                # conta ativa — este último não pode derrubar a varredura.
                continue

            # Renderizado uma vez por (alerta, canal): o texto é o mesmo para
            # todos os destinatários daquele canal, e o do outro canal é outro
            # texto (emoji e `*negrito*` num, texto puro com assunto no outro).
            mensagem = canal.renderizar(alerta.tipo, alerta.contexto, settings)
            for destinatario in destinatarios:
                _despachar_para(
                    session,
                    settings,
                    canal,
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
    canal: CanalAlerta,
    alerta: Alerta,
    *,
    mensagem: MensagemAlerta,
    destinatario: Destinatario,
    inicio_cooldown: datetime,
    inicio_janela_rate_limit: datetime,
    resumo: ResumoVarredura,
) -> None:
    """Um alerta, um canal, um destinatário: cooldown, rate limit, envio e registro."""
    if repository.existe_envio_recente(
        session,
        tipo=alerta.tipo,
        chave=alerta.chave,
        destinatario=destinatario.endereco,
        desde=inicio_cooldown,
    ):
        # Supressão silenciosa, sem linha no banco — ver a docstring do módulo.
        # A chave é o ENDEREÇO: o mesmo aviso pode sair nos dois canais, mas
        # não duas vezes no mesmo canal dentro da janela.
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
            canal=canal.canal,
            chave=alerta.chave,
            destinatario=destinatario.endereco,
            usuario_id=destinatario.usuario_id,
            mensagem=mensagem.para_registro(),
            status=StatusAlerta.SUPRIMIDO,
            detalhe=f"rate limit: {enviados_na_hora} envios na última hora (limite {limite})",
            documento_id=alerta.documento_id,
        )
        resumo.suprimidos += 1
        return

    try:
        canal.enviar(destinatario.endereco, mensagem)
    except EnvioError as exc:
        # A falha de um destinatário não pode cancelar os outros: os alertas
        # restantes são de outras pessoas, de outros problemas e — desde o ADR
        # 0006 — de outro canal, que pode estar inteiro.
        logger.warning(
            "falha ao enviar alerta %s pelo canal %s: %s",
            alerta.tipo.value,
            canal.canal.value,
            exc,
        )
        repository.registrar(
            session,
            tipo=alerta.tipo,
            canal=canal.canal,
            chave=alerta.chave,
            destinatario=destinatario.endereco,
            usuario_id=destinatario.usuario_id,
            mensagem=mensagem.para_registro(),
            status=StatusAlerta.FALHA,
            detalhe=str(exc),
            documento_id=alerta.documento_id,
        )
        resumo.falhas += 1
        return

    repository.registrar(
        session,
        tipo=alerta.tipo,
        canal=canal.canal,
        chave=alerta.chave,
        destinatario=destinatario.endereco,
        usuario_id=destinatario.usuario_id,
        mensagem=mensagem.para_registro(),
        status=StatusAlerta.ENVIADO,
        documento_id=alerta.documento_id,
    )
    resumo.enviados += 1


def _estado_dos_canais(canais: Sequence[CanalAlerta]) -> dict[str, EstadoCanal]:
    """As duas perguntas de cada canal, sempre com todos eles (ver `ResumoVarredura`)."""
    return {
        canal.canal.value: EstadoCanal(habilitado=canal.habilitado, disponivel=canal.disponivel())
        for canal in canais
    }


def _whatsapp_configurado(resumo: ResumoVarredura) -> bool:
    """Preserva o significado histórico de `provider_configurado` — ver `schema.py`."""
    estado = resumo.canais.get(Canal.WHATSAPP.value)
    return estado is not None and estado.habilitado and estado.disponivel


def _contar_por_tipo(alertas: Sequence[Alerta]) -> dict[str, int]:
    """Contagem por tipo, sempre com os quatro tipos (ver `ResumoVarredura`)."""
    contagem = Counter(alerta.tipo.value for alerta in alertas)
    return {tipo.value: contagem.get(tipo.value, 0) for tipo in TipoAlerta}
