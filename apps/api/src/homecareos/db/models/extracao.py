"""Model da extração de campos de um documento, feita por IA (Claude) ou outro provider.

DESVIO CONSCIENTE da issue #2 original: a issue pedia um campo `raw_response`
(JSONB) direto nesta tabela. Isso não foi implementado — o raw response de
extração carrega prontuário clínico identificável, e guardá-lo no Postgres o
replicaria em todo backup/dump, além de tornar o expurgo um `UPDATE` em vez
de uma deleção de objeto. O raw vai para o S3/MinIO com SSE, sob a chave
`extracoes/<documento_id>/<sha256>.json`; o banco guarda só essa chave, em
`raw_response_ref`. Mesmo padrão conceitual de `ProtectedRawResponseStore`
(ver croquito/services/worker/src/croquito_worker/local_queue.py:837-879):
banco guarda só a referência, o blob vai pro object storage.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class Extracao(Base):
    """Resultado da extração assistida por IA dos campos de um documento."""

    __tablename__ = "extracoes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    documento_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documentos.id"), nullable=False
    )
    campos_extraidos: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confianca: Mapped[float] = mapped_column(Float, nullable=False)
    confianca_por_campo: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Referência ao blob no S3/MinIO (`extracoes/<documento_id>/<sha256>.json`),
    # nunca o conteúdo bruto — ver desvio consciente documentado acima.
    raw_response_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    modelo: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
