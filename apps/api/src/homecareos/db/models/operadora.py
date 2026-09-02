"""Model da operadora (convênio/plano) — dona das regras de conferência e do config de matching."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class Operadora(Base):
    """Convênio/operadora de saúde para o qual o home care presta serviço."""

    __tablename__ = "operadoras"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nome: Mapped[str] = mapped_column(String, nullable=False)
    codigo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Nullable: o seed inicial (`homecareos.seed`) cadastra operadoras só por
    # nome/código; o config de matching de regras é preenchido depois.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
