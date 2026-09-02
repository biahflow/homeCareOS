"""Auditoria do raw response do modelo, fora do banco.

O raw response de uma extração contém prontuário clínico identificável e por
isso não vai para o Postgres — só a chave de onde ele foi guardado vai. Este
módulo define a porta (`RawResponseStore`) e uma implementação em memória para
os testes; a implementação real em S3 é ligada na Fase 2, usando
`homecareos/storage.py` — que esta trilha não importa, porque outra trilha o
está escrevendo agora.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class RawResponseStore(Protocol):
    """Porta de persistência do payload cru de uma extração."""

    def persist(self, documento_id: str, payload: dict[str, Any]) -> str:
        """Persiste `payload` e devolve a chave onde ele foi guardado.

        Ex.: ``"extracoes/<documento_id>/<sha256>.json"``.
        """
        ...


def _payload_key(documento_id: str, payload: dict[str, Any]) -> str:
    """Chave determinística: mesmo documento + mesmo payload → mesma chave."""
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"extracoes/{documento_id}/{digest}.json"


@dataclass
class InMemoryRawResponseStore:
    """Fake para os testes: guarda o payload num dicionário em processo."""

    _dados: dict[str, dict[str, Any]] = field(default_factory=dict)

    def persist(self, documento_id: str, payload: dict[str, Any]) -> str:
        key = _payload_key(documento_id, payload)
        self._dados[key] = payload
        return key

    def get(self, key: str) -> dict[str, Any]:
        return self._dados[key]
