"""`RawResponseStore` em S3/MinIO — a implementação real que a Fase 2 liga.

O raw response de uma extração carrega prontuário clínico identificável. Ele
não entra no Postgres (seria replicado em todo backup e o expurgo viraria um
`UPDATE`): vai para o object storage sob
`extracoes/<documento_id>/<sha256>.json`, e o banco guarda só essa chave.

A chave é determinística sobre o conteúdo gravado: reprocessar o mesmo
documento com a mesma resposta sobrescreve o mesmo objeto em vez de acumular
cópias do mesmo prontuário.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from homecareos.storage import DocumentStorage

CONTENT_TYPE = "application/json"


@dataclass
class S3RawResponseStore:
    """Persiste o payload cru da extração via `DocumentStorage`."""

    storage: DocumentStorage

    def persist(self, documento_id: str, payload: dict[str, Any]) -> str:
        corpo = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(corpo).hexdigest()
        chave = f"extracoes/{documento_id}/{digest}.json"
        return self.storage.put(chave, corpo, CONTENT_TYPE)
