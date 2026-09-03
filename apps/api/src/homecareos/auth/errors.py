"""Erros de domínio da autenticação.

Eles existem para o serviço não precisar levantar `HTTPException` — quem
traduz erro de domínio em status HTTP é o router, como em `intake/errors.py` e
`classification/errors.py`.

A mensagem destes erros é para log e para o CLI, **nunca** para o corpo de uma
resposta de login: distinguir "e-mail não existe" de "senha errada" na resposta
entrega a lista de quem trabalha na operação a quem estiver sondando.
"""

from __future__ import annotations


class AuthError(Exception):
    """Base de toda falha de autenticação/identidade."""


class CredencialInvalidaError(AuthError):
    """E-mail inexistente, senha errada ou usuário inativo — os três, sem distinção."""


class SessaoInvalidaError(AuthError):
    """Sessão ausente, expirada, revogada ou de usuário desativado."""
