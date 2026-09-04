"""Janelas de segurança que cada tabela expurgada precisa respeitar — issue #39.

`tentativas_login`, `tokens_recuperacao` e `alertas_enviados` não são só log:
as três são consultadas por freios de segurança ativos, dentro de janelas de
tempo. Apagar uma linha antes que a janela do freio que a consulta tenha
passado desarma esse freio. Este módulo é a fonte única dessas janelas —
inclusive das duas que são constantes hardcoded, e não configuração:

- `tentativas_login`: `auth.protecao._contar_falhas`, via `avaliar_bloqueio`,
  janela `Settings.login_janela_minutos`.
- `tokens_recuperacao`: `auth.recuperacao.emissoes_recentes`, janela
  `JANELA_DO_TETO`, hardcoded em `auth/recuperacao.py`.
- `alertas_enviados`: `alerts.repository.existe_envio_recente`, janela
  `Settings.alertas_cooldown_horas` (cooldown).
- `alertas_enviados`: `alerts.repository.contar_envios_desde`, janela
  `JANELA_RATE_LIMIT`, hardcoded em `alerts/service.py` (rate limit).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homecareos.alerts.service import JANELA_RATE_LIMIT
from homecareos.auth.recuperacao import JANELA_DO_TETO
from homecareos.config import Settings

# Margem de segurança sobre a janela mínima. A janela em si é o piso
# ABSOLUTO — abaixo dele o freio já está desarmado; rodar exatamente nela é
# apostar contra o relógio (job atrasado, `agora` do expurgo levemente à
# frente do que o freio ainda precisa enxergar, retenção configurada minutos
# acima do limite por engano). Multiplicar por 2, em vez de somar um offset
# fixo, dá folga PROPORCIONAL a cada janela: um offset fixo de, digamos, 1h
# dominaria a janela de 15 min de `tentativas_login` (a margem viraria 4x a
# janela) e seria irrelevante nas 24h de `alertas_enviados` (a margem seria só
# +4%).
FATOR_MARGEM_SEGURANCA = 2


@dataclass(frozen=True)
class JanelaSeguranca:
    """Uma janela de tempo que algum freio de segurança consulta na tabela."""

    descricao: str
    janela: timedelta
    origem: str

    @property
    def piso_minimo(self) -> timedelta:
        """Retenção mínima aceitável: a janela em si, com a margem de segurança."""
        return self.janela * FATOR_MARGEM_SEGURANCA


def janelas_tentativas_login(settings: Settings) -> list[JanelaSeguranca]:
    return [
        JanelaSeguranca(
            descricao="janela de observação de falhas de login (trava de IP e de conta)",
            janela=timedelta(minutes=settings.login_janela_minutos),
            origem=("Settings.login_janela_minutos (config.py) — auth/protecao.py:111-133,183-196"),
        )
    ]


def janelas_tokens_recuperacao(settings: Settings) -> list[JanelaSeguranca]:
    del settings  # a janela de tokens_recuperacao é hardcoded, não configuração.
    return [
        JanelaSeguranca(
            descricao="janela do teto de emissão de tokens de recuperação",
            janela=JANELA_DO_TETO,
            origem=("JANELA_DO_TETO, hardcoded em auth/recuperacao.py:35 — emissoes_recentes"),
        )
    ]


def janelas_alertas_enviados(settings: Settings) -> list[JanelaSeguranca]:
    return [
        JanelaSeguranca(
            descricao="cooldown de reenvio do mesmo alerta ao mesmo destinatário",
            janela=timedelta(hours=settings.alertas_cooldown_horas),
            origem="Settings.alertas_cooldown_horas (config.py) — alerts/repository.py:53-75",
        ),
        JanelaSeguranca(
            descricao="rate limit de alertas por destinatário",
            janela=JANELA_RATE_LIMIT,
            origem=(
                "JANELA_RATE_LIMIT, hardcoded em alerts/service.py:44 — alerts/repository.py:78-90"
            ),
        ),
    ]
