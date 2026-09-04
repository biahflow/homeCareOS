"""Erros do expurgo por retenção — issue #39."""

from __future__ import annotations


class RetencaoError(Exception):
    """Base de qualquer falha do expurgo por retenção."""


class RetencaoInvalidaError(RetencaoError):
    """A retenção configurada é menor que o piso mínimo da tabela — a janela de
    um freio de segurança ativo, ou o valor de auditoria que a tabela existe
    para preservar. Ver `retencao/janelas.py`.

    Erro de configuração, não falha transitória: quem chama precisa corrigir
    a retenção antes de rodar de novo. Nada é apagado quando esta exceção é
    levantada — `retencao/service.py` valida todas as tabelas selecionadas
    antes de apagar qualquer uma.
    """


class RetencaoConfigError(RetencaoError):
    """Argumento ou configuração inválida: tabela desconhecida, lote não positivo."""
