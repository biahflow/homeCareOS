"""Model de pendência aberta sobre um documento, até a equipe corrigir e liberar."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base
from homecareos.db.models.enums import PendenciaStatus


class Pendencia(Base):
    """Pendência aberta sobre um `Documento` durante a conferência."""

    __tablename__ = "pendencias"
    __table_args__ = (Index("ix_pendencias_deadline", "deadline"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    documento_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documentos.id"), nullable=False
    )
    tipo_problema: Mapped[str] = mapped_column(String, nullable=False)
    descricao: Mapped[str] = mapped_column(String, nullable=False)
    responsavel: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[PendenciaStatus] = mapped_column(
        SAEnum(
            PendenciaStatus,
            name="pendencia_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=PendenciaStatus.ABERTA,
        index=True,
    )
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Nullable: só é preenchido quando a pendência é de fato resolvida.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
