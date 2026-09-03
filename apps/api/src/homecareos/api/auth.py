"""Autenticação por `X-API-Key` para toda a API — a parte crítica desta trilha.

O dado que trafega em `/api/*` é prontuário clínico. Antes desta trilha,
`POST /api/documentos` estava completamente aberto.

Regras não negociáveis, cada uma com uma razão concreta:

- Comparação com `hmac.compare_digest`, nunca `==`: comparação de string comum
  tem *short-circuit* no primeiro caractere diferente, e o tempo de resposta
  vaza quantos caracteres do prefixo estavam certos.
- Chave ausente e chave inválida devolvem o **mesmo** status e o **mesmo**
  corpo. Diferenciá-los ("chave não enviada" vs. "chave errada") entrega
  informação a quem está sondando o endpoint.
- A chave recebida nunca é logada — nem parcialmente, nem em mensagem de erro.
- Esta dependência é aplicada **por router**, no `include_router(...,
  dependencies=[...])` em `main.py` — nunca endpoint a endpoint. Um endpoint
  novo nasce protegido por construção, sem depender de alguém lembrar de
  proteger cada rota individualmente.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from homecareos.config import Settings, get_settings

MENSAGEM_CREDENCIAL_INVALIDA = "credencial inválida"

# `auto_error=False`: com o padrão (`True`) o FastAPI responderia 403 sozinho
# quando o header está ausente, um status e um corpo diferentes dos usados
# quando a chave está presente mas errada. O tratamento unificado abaixo é o
# que garante o mesmo corpo/status para os dois casos.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _chaves_validas(settings: Settings) -> list[str]:
    """`settings.api_keys` é uma lista separada por vírgula, para rotação sem downtime."""
    return [chave.strip() for chave in settings.api_keys.split(",") if chave.strip()]


def _bate_com_alguma_chave(recebida: str, validas: list[str]) -> bool:
    """Compara em bytes, nunca em `str`.

    `hmac.compare_digest` levanta `TypeError` quando recebe `str` com
    caractere não-ASCII — e a chave vem de um header controlado por quem
    chama. Comparando `str`, um `X-API-Key: café` viraria 500 em vez de 401:
    uma resposta distinguível, exatamente a informação que a unificação
    ausente/inválida existe para não entregar. Em bytes o problema não
    existe, e a comparação segue de tempo constante.
    """
    recebida_bytes = recebida.encode("utf-8")
    return any(hmac.compare_digest(recebida_bytes, valida.encode("utf-8")) for valida in validas)


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Depends(_api_key_header)] = None,
) -> None:
    """Dependency de auth: exige `X-API-Key` presente e igual a uma chave configurada.

    Nunca revela, pela resposta, se a chave estava ausente ou apenas errada.
    """
    if x_api_key is None or not _bate_com_alguma_chave(x_api_key, _chaves_validas(settings)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=MENSAGEM_CREDENCIAL_INVALIDA,
        )
