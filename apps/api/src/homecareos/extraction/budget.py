"""Teto de custo por lote de extração.

Um PDF grande no fechamento de competência vira centenas de chamadas de Vision.
`CostBudget.reserve()` é chamado **antes** de cada chamada e reserva o custo
estimado de forma pessimista; se a reserva estourar o teto, levanta
`BudgetExceededError` e a chamada nunca acontece. Mesmo padrão de
`croquito_worker.providers.CostBudget` (reserva antes, nunca depois).
"""

from __future__ import annotations

from dataclasses import dataclass

from homecareos.extraction.errors import BudgetExceededError


@dataclass
class CostBudget:
    """Reserva pessimista de custo, compartilhada por todas as chamadas de um lote."""

    max_usd: float
    cost_per_call_usd: float
    spent_usd: float = 0.0

    def reserve(self) -> None:
        """Reserva o custo de uma chamada; levanta `BudgetExceededError` se estourar.

        A reserva acontece antes da chamada de propósito: é isso que impede a
        chamada de sair, não uma checagem feita depois do gasto já ter ocorrido.
        """
        if self.spent_usd + self.cost_per_call_usd > self.max_usd:
            raise BudgetExceededError(
                f"orçamento do lote esgotado: gasto={self.spent_usd:.4f} "
                f"+ chamada={self.cost_per_call_usd:.4f} > teto={self.max_usd:.4f}"
            )
        self.spent_usd += self.cost_per_call_usd
