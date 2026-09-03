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
    # Rótulo legível de quem agiu: o e-mail da pessoa, ou `"api"`/`"sistema"`
    # para ação de máquina. Continua sendo a coluna que se lê no histórico.
    usuario: Mapped[str] = mapped_column(String, nullable=False)
    # A identidade referencial de quem agiu, quando havia uma pessoa. Nullable
    # de propósito, e são dois casos legítimos: linha histórica (anterior à
    # issue #30) e ação de máquina (`"api"` da chave de integração, `"sistema"`
    # do dispatcher de extração). Forjar um id nesses casos faria a auditoria
    # apontar para alguém que não fez nada — o oposto do que ela existe para
    # fazer.
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=True
    )
    detalhe: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
