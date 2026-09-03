"""Erros de negócio da classificação e do ciclo de resolução de pendências."""

from __future__ import annotations


class ClassificationError(Exception):
    """Erro de negócio na classificação de um documento ou na sua revalidação."""


class TransicaoInvalidaError(ClassificationError):
    """A transição de status pedida não existe na máquina de estados do documento."""


class DocumentoNaoEncontradoError(ClassificationError):
    """Não existe documento com o id informado."""


class RevalidacaoIndisponivelError(ClassificationError):
    """Falta um insumo para revalidar (operadora, extração ou regras ativas)."""
