"""Convenção de resposta de erro compartilhada por toda a API.

Todo erro — autenticação, validação, erro de domínio ou HTTP genérico — sai
no mesmo envelope, para o consumidor tratar erro de um jeito só:

```json
{"error": {"tipo": "unprocessable_entity", "mensagem": "...", "detalhes": {...}}}
```

Quem registra os exception handlers que produzem este envelope é
`homecareos.api.errors`; este módulo só define a forma.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErroDetalhe(BaseModel):
    tipo: str
    mensagem: str
    detalhes: Any = None


class ErroResponse(BaseModel):
    """Corpo de toda resposta de erro da API."""

    error: ErroDetalhe


def erro_envelope(*, tipo: str, mensagem: str, detalhes: Any = None) -> dict[str, Any]:
    detalhe = ErroDetalhe(tipo=tipo, mensagem=mensagem, detalhes=detalhes)
    return ErroResponse(error=detalhe).model_dump(mode="json")
