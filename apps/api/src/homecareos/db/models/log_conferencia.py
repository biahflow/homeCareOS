"""Model do log de auditoria de ações tomadas durante a conferência de um documento."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class LogConferencia(Base):
    """Registro de auditoria de uma ação tomada sobre um `Documento` durante a conferência."""

    __tablename__ = "log_conferencia"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    documento_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documentos.id"), nullable=False
    )
    acao: Mapped[str] = mapped_column(String, nullable=False)
    usuario: Mapped[str] = mapped_column(String, nullable=False)
    detalhe: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
