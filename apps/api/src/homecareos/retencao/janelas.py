"""Pisos mínimos de retenção que cada tabela expurgada precisa respeitar — issue #39.

Há **duas naturezas de piso**, e a diferença entre elas não é detalhe de
implementação — é a razão pela qual cada tabela recusa uma retenção curta:

- **Piso de freio de segurança** (`JanelaSeguranca`). `tentativas_login`,
  `tokens_recuperacao` e `alertas_enviados` não são só log: as três são
  consultadas por freios de segurança ativos, dentro de janelas de tempo.
  Apagar uma linha antes que a janela do freio que a consulta tenha passado
  **desarma esse freio**. O piso é a janela real do consumidor, com a margem
  de `FATOR_MARGEM_SEGURANCA`.
- **Piso de valor de auditoria** (`PisoValorAuditoria`). `auditoria_usuarios`
  e `auditoria_canais_alerta` não são consultadas por freio nenhum — os únicos
  leitores são `GET /api/usuarios/auditoria` (`auth/auditoria_router.py`) e
  `GET /api/alertas/canais/auditoria` (`alerts/canais_router.py`), e nenhuma
  decisão de segurança depende de elas estarem completas nos últimos N minutos.
  Mesmo assim tem piso, por outra razão: uma auditoria administrativa curta
  demais deixa de responder à única pergunta que a justifica ("quem deu a
  esta pessoa o papel de coordenador, e quando?"), que aparece em
  investigação — raramente no mesmo trimestre do evento. Aqui o mínimo é
  **declarado**, não medido de um consumidor, e vai **sem** a margem
  multiplicativa (ver `FATOR_MARGEM_SEGURANCA`).

Este módulo é a fonte única desses pisos — inclusive das janelas que são
constantes hardcoded, e não configuração:

- `tentativas_login`: `auth.protecao._contar_falhas`, via `avaliar_bloqueio`,
  janela `Settings.login_janela_minutos`.
- `tokens_recuperacao`: `auth.recuperacao.emissoes_recentes`, janela
  `JANELA_DO_TETO`, hardcoded em `auth/recuperacao.py`.
- `alertas_enviados`: `alerts.repository.existe_envio_recente`, janela
  `Settings.alertas_cooldown_horas` (cooldown).
- `alertas_enviados`: `alerts.repository.contar_envios_desde`, janela
  `JANELA_RATE_LIMIT`, hardcoded em `alerts/service.py` (rate limit).
- `auditoria_usuarios`: nenhum freio — piso de propósito,
  `MINIMO_AUDITORIA_USUARIOS`.
- `auditoria_canais_alerta`: nenhum freio — piso de propósito,
  `MINIMO_AUDITORIA_CANAIS`.
- `consumos_rate_limit`: `limites.protecao.avaliar_limite`, janela `JANELA`,
  hardcoded em `limites/protecao.py` (ADR 0005).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from homecareos.alerts.service import JANELA_RATE_LIMIT
from homecareos.auth.recuperacao import JANELA_DO_TETO
from homecareos.config import Settings
from homecareos.limites.protecao import JANELA as JANELA_RATE_LIMIT_ROTAS

# Margem de segurança sobre a janela mínima. A janela em si é o piso
# ABSOLUTO — abaixo dele o freio já está desarmado; rodar exatamente nela é
# apostar contra o relógio (job atrasado, `agora` do expurgo levemente à
# frente do que o freio ainda precisa enxergar, retenção configurada minutos
# acima do limite por engano). Multiplicar por 2, em vez de somar um offset
# fixo, dá folga PROPORCIONAL a cada janela: um offset fixo de, digamos, 1h
# dominaria a janela de 15 min de `tentativas_login` (a margem viraria 4x a
# janela) e seria irrelevante nas 24h de `alertas_enviados` (a margem seria só
# +4%).
#
# A margem vale só para piso de FREIO: ela compra folga contra o relógio de um
# consumidor real. Num piso declarado por propósito (`PisoValorAuditoria`) não
# há relógio a perder — o mínimo é o mínimo, e dobrá-lo em silêncio seria
# inventar política de retenção, não proteger freio nenhum.
FATOR_MARGEM_SEGURANCA = 2

# Piso de valor de auditoria de `auditoria_usuarios`: um ano. Abaixo disso a
# tabela não cobre um ciclo de auditoria e deixa de servir ao propósito.
#
# **Assunção deste time, não requisito confirmado pelo cliente ou pelo
# jurídico** — como os defaults de `Settings.retencao_*_dias`. A diferença é
# que este número NÃO é configuração, de propósito: um piso que a mesma pessoa
# pode baixar junto com a retenção que ele limita não é piso, é sugestão.
# Mudá-lo é mudar política, e política se muda em code review.
MINIMO_AUDITORIA_USUARIOS = timedelta(days=365)

# Piso de valor de auditoria de `auditoria_canais_alerta` (ADR 0006): um ano, o
# mesmo de `MINIMO_AUDITORIA_USUARIOS` e pela mesma razão — a pergunta que esta
# tabela responde ("quem desligou o canal, e desde quando ninguém está sendo
# avisado?") aparece em investigação, muito depois do evento.
#
# **Assunção deste time, não requisito confirmado pelo cliente ou pelo
# jurídico.** Alinhar com a auditoria administrativa em vez de inventar um
# segundo número é deliberado: não há dado que sustente uma política diferente,
# e dois pisos distintos sem razão distinta viram folclore. Como o outro, NÃO é
# configuração — um piso que quem configura a retenção pode baixar junto com ela
# não é piso.
#
# Esta tabela é a mais leve das duas em dado pessoal: guarda o e-mail do ator
# (funcionário), e nenhum `alvo_email` de terceiro.
MINIMO_AUDITORIA_CANAIS = timedelta(days=365)


class PisoRetencao(Protocol):
    """O que o expurgo precisa saber de um piso: o mínimo, e a razão dele.

    A razão é do piso, não de quem valida: `retencao/service.py` monta a
    recusa a partir de `motivo()` e não conhece as naturezas — acrescentar uma
    terceira não passa por lá.
    """

    @property
    def piso_minimo(self) -> timedelta: ...

    def motivo(self) -> str:
        """A oração que completa 'o mínimo aceitável (X) ...' na recusa."""
        ...


@dataclass(frozen=True)
class JanelaSeguranca:
    """Piso de FREIO: uma janela de tempo que algum freio de segurança consulta na tabela."""

    descricao: str
    janela: timedelta
    origem: str

    @property
    def piso_minimo(self) -> timedelta:
        """Retenção mínima aceitável: a janela em si, com a margem de segurança."""
        return self.janela * FATOR_MARGEM_SEGURANCA

    def motivo(self) -> str:
        return (
            f"para não desarmar '{self.descricao}' (janela ativa: {self.janela}, "
            f"origem: {self.origem}, margem de segurança aplicada: {FATOR_MARGEM_SEGURANCA}x)"
        )


@dataclass(frozen=True)
class PisoValorAuditoria:
    """Piso de PROPÓSITO: o mínimo abaixo do qual a tabela deixa de servir ao que a justifica.

    Sem janela de consumidor e sem `FATOR_MARGEM_SEGURANCA`: `minimo` já é o
    piso — ver a docstring do módulo.
    """

    descricao: str
    minimo: timedelta
    origem: str

    @property
    def piso_minimo(self) -> timedelta:
        return self.minimo

    def motivo(self) -> str:
        return (
            f"porque abaixo disto {self.descricao} (mínimo declarado: {self.minimo}, "
            f"aplicado sem margem multiplicativa; origem: {self.origem})"
        )


def pisos_tentativas_login(settings: Settings) -> list[PisoRetencao]:
    return [
        JanelaSeguranca(
            descricao="janela de observação de falhas de login (trava de IP e de conta)",
            janela=timedelta(minutes=settings.login_janela_minutos),
            origem=("Settings.login_janela_minutos (config.py) — auth/protecao.py:111-133,183-196"),
        )
    ]


def pisos_tokens_recuperacao(settings: Settings) -> list[PisoRetencao]:
    del settings  # a janela de tokens_recuperacao é hardcoded, não configuração.
    return [
        JanelaSeguranca(
            descricao="janela do teto de emissão de tokens de recuperação",
            janela=JANELA_DO_TETO,
            origem=("JANELA_DO_TETO, hardcoded em auth/recuperacao.py:35 — emissoes_recentes"),
        )
    ]


def pisos_alertas_enviados(settings: Settings) -> list[PisoRetencao]:
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


def pisos_auditoria_usuarios(settings: Settings) -> list[PisoRetencao]:
    del settings  # o piso de propósito é declarado, não derivado de configuração.
    return [
        PisoValorAuditoria(
            descricao=(
                "a auditoria administrativa deixa de responder à pergunta que a justifica "
                "existir — quem autorizou o quê, em quem e quando —, que aparece em "
                "investigação, muito depois do evento"
            ),
            minimo=MINIMO_AUDITORIA_USUARIOS,
            origem=(
                "MINIMO_AUDITORIA_USUARIOS, declarado em retencao/janelas.py — "
                "não é configuração, ver a docstring do módulo"
            ),
        )
    ]


def pisos_auditoria_canais(settings: Settings) -> list[PisoRetencao]:
    del settings  # o piso de propósito é declarado, não derivado de configuração.
    return [
        PisoValorAuditoria(
            descricao=(
                "a auditoria dos canais deixa de responder à pergunta que a justifica "
                "existir — quem desligou o canal, e desde quando a operação está sem "
                "aviso —, que aparece em investigação, muito depois do evento"
            ),
            minimo=MINIMO_AUDITORIA_CANAIS,
            origem=(
                "MINIMO_AUDITORIA_CANAIS, declarado em retencao/janelas.py — "
                "não é configuração, ver a docstring do módulo"
            ),
        )
    ]


def pisos_consumos_rate_limit(settings: Settings) -> list[PisoRetencao]:
    del settings  # a janela do freio é constante, não configuração.
    return [
        JanelaSeguranca(
            descricao="janela de contagem do rate limit das rotas caras (ADR 0005)",
            janela=JANELA_RATE_LIMIT_ROTAS,
            origem=("JANELA, hardcoded em limites/protecao.py — limites/protecao.avaliar_limite"),
        )
    ]
