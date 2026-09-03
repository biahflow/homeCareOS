"""Erros de negócio do motor de regras."""

from __future__ import annotations


class RegraError(Exception):
    """Erro de negócio na definição ou avaliação de uma regra de operadora."""


class CondicaoInvalidaError(RegraError):
    """A `condicao` submetida não corresponde à gramática declarativa esperada."""


class RegraNaoEncontradaError(RegraError):
    """Não existe regra (ativa ou inativa) com o id informado."""
