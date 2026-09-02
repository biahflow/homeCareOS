"""Retry do provider Anthropic, feito à mão fora do SDK.

O cliente Anthropic é construído com `max_retries=0` (ver `claude.py`): o retry
embutido do SDK multiplicaria o tempo de bloqueio de um worker síncrono a cada
chamada, e quem decide quanto tempo vale a pena esperar é este módulo, não a
biblioteca. Mesma decisão de `pulse/backend/apps/core/ai.py:368-378`.

A insistência é limitada por **prazo de parede** (`deadline_seconds`, default
300s), não por contagem de tentativas: uma tentativa que trava no timeout de
60s custa muito mais tempo que uma que volta com 429 em ~1s, e só um prazo
descreve as duas com o comportamento certo. Duas escadas de backoff, por
família de erro — mesmo desenho de
`croquito_worker.providers.RetryingProviderAdapter`:

- `RateLimitError`: 5s → 60s (dobrando, com jitter) — throttle real volta devagar;
- `APITimeoutError` / `APIStatusError(5xx)` / `APIConnectionError`: 250ms → 2s —
  falha de transporte tende a se resolver rápido, ou não se resolve.

`BadRequestError`/4xx, recusa do classificador e `BudgetExceededError` nunca são
retentados: insistir não busca disponibilidade, busca uma leitura diferente do
mesmo documento — e isso o retry não pode inventar.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

import anthropic

from homecareos.extraction.errors import BudgetExceededError, ExtractionRefusedError

T = TypeVar("T")

DEADLINE_SECONDS_DEFAULT = 300.0

# Escada curta: falha de transporte (timeout, 5xx, conexão).
SHORT_BACKOFF_BASE_SECONDS = 0.25
SHORT_BACKOFF_CAP_SECONDS = 2.0

# Escada longa: rate limit.
LONG_BACKOFF_BASE_SECONDS = 5.0
LONG_BACKOFF_CAP_SECONDS = 60.0

# Fração da espera sorteada na escada longa, para dispersar chamadas que
# levaram 429 no mesmo instante sem afrouxar a espera em si.
JITTER_FRACTION = 0.25

# Teto de segurança de tentativas: cobre o caso degenerado de falha instantânea
# em laço (sem ele, o prazo de parede sozinho não limitaria o número de voltas).
ATTEMPT_CEILING = 20

_RETRYABLE_SHORT = (
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
)


def _is_retryable(error: Exception) -> tuple[bool, bool]:
    """Devolve `(retentável, é_escada_longa)` para `error`.

    `BadRequestError` é uma subclasse de `APIStatusError` com `status_code`
    4xx — por isso a checagem de "5xx" vem antes de aceitar `APIStatusError`
    genericamente, e `BadRequestError` nunca cai nela.
    """
    if isinstance(error, anthropic.RateLimitError):
        return True, True
    if isinstance(error, _RETRYABLE_SHORT):
        return True, False
    if isinstance(error, anthropic.APIStatusError) and not isinstance(
        error, anthropic.BadRequestError
    ):
        if error.status_code >= 500:
            return True, False
        return False, False
    return False, False


def _default_jitter() -> float:
    return random.random()


@dataclass
class RetryPolicy:
    """Executor de retry com prazo de parede e backoff por família de erro.

    Relógio, espera e sorteio são seams injetáveis, para os testes não
    dependerem de tempo real.
    """

    deadline_seconds: float = DEADLINE_SECONDS_DEFAULT
    attempt_ceiling: int = ATTEMPT_CEILING
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = field(default=_default_jitter)

    def _backoff_seconds(self, *, long_ladder: bool, attempt: int) -> float:
        doubling = float(2 ** (attempt - 1))
        if not long_ladder:
            return min(SHORT_BACKOFF_BASE_SECONDS * doubling, SHORT_BACKOFF_CAP_SECONDS)
        base = min(LONG_BACKOFF_BASE_SECONDS * doubling, LONG_BACKOFF_CAP_SECONDS)
        return base * (1.0 + JITTER_FRACTION * self.jitter())

    def run(self, call: Callable[[], T]) -> T:
        """Executa `call()`, retentando só as famílias transitórias de erro.

        `ExtractionRefusedError` e `BudgetExceededError` são erros de domínio
        que `call()` pode levantar diretamente (não vêm do SDK) e nunca são
        retentados — verificados aqui para não depender de `call()` nunca
        levantá-los por engano dentro de uma tentativa que seria reintentada.
        """
        started = self.now()
        attempt = 0
        while True:
            attempt += 1
            try:
                return call()
            except (ExtractionRefusedError, BudgetExceededError):
                raise
            except Exception as error:
                retryable, long_ladder = _is_retryable(error)
                give_up = not retryable or attempt >= self.attempt_ceiling
                delay = (
                    0.0
                    if give_up
                    else self._backoff_seconds(long_ladder=long_ladder, attempt=attempt)
                )
                if not give_up:
                    remaining = self.deadline_seconds - (self.now() - started)
                    give_up = remaining <= 0.0 or delay > remaining
                if give_up:
                    raise
                self.sleep(delay)
