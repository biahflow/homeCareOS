"""Formas de entrada e de saída do endpoint de upload.

A forma da resposta é contrato com o frontend, que já a consome:

```json
{"documentos": [{"id": "<uuid>", "pagina": 1, "status": "processando",
                 "competencia": "2024-03"}]}
```
"""

from __future__ import annotations

import re
import uuid

from pydantic import BaseModel

from homecareos.db.models import DocumentoStatus
from homecareos.intake.repository import DocumentoRegistrado

COMPETENCIA_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
"""`YYYY-MM`, com mês entre 01 e 12. `2024-3` e `03/2024` não passam."""


def competencia_valida(competencia: str) -> bool:
    return COMPETENCIA_REGEX.fullmatch(competencia) is not None


class DocumentoCriado(BaseModel):
    """Um documento (uma página) criado pelo upload."""

    id: uuid.UUID
    pagina: int
    status: DocumentoStatus
    competencia: str

    @classmethod
    def de_registrado(cls, documento: DocumentoRegistrado) -> DocumentoCriado:
        return cls(
            id=documento.id,
            pagina=documento.pagina,
            status=documento.status,
            competencia=documento.competencia,
        )


class UploadResponse(BaseModel):
    """Resposta do `POST /api/documentos`."""

    documentos: list[DocumentoCriado]
