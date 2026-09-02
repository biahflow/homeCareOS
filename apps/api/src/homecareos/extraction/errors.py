"""Erros do pipeline de extração.

A distinção entre estas classes é o que permite ao chamador (e ao retry, ver
`retry.py`) diferenciar "o modelo recusou" de "a resposta veio cortada" de "o
provider caiu" — cada uma pede uma reação diferente, e nenhuma delas deve ser
tentada de novo esperando um resultado diferente na mesma pergunta.
"""

from __future__ import annotations


class ExtractionError(Exception):
    """Base de qualquer falha de extração."""


class ExtractionRefusedError(ExtractionError):
    """O classificador de segurança do modelo recusou a requisição.

    Isto chega como HTTP 200 com `stop_reason == "refusal"`, não como exceção
    do SDK — por isso precisa ser detectado explicitamente antes de ler
    `response.content` ou `response.parsed_output` (ver `claude.py`). Nunca é
    retentado: a próxima tentativa recusaria de novo pelo mesmo motivo.
    """

    def __init__(self, category: str | None = None) -> None:
        self.category = category
        super().__init__(f"extração recusada pelo classificador de segurança: {category}")


class ExtractionIncompleteError(ExtractionError):
    """A resposta foi cortada por `max_tokens` antes de terminar.

    `max_tokens=8192` cobre thinking + resposta somados; quando a soma estoura,
    o modelo para no meio do JSON e isto não é um sucesso silencioso — é uma
    extração incompleta, que precisa aparecer como falha e não como
    `ExtractionResult` com campos faltando por engano.
    """


class BudgetExceededError(ExtractionError):
    """O teto de custo do lote foi atingido antes desta chamada.

    Levantado por `budget.py` **antes** de qualquer chamada à API — a API nunca
    chega a ser chamada. Nunca é retentado: insistir não abre orçamento novo.
    """
