"""Model do documento (evolução, ficha, boletim, matmed) em conferência pré-faturamento."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base
from homecareos.db.models.enums import DocumentoStatus, TipoDocumento


class Documento(Base):
    """Documento ingerido para a conferência pré-faturamento.

    Ciclo de vida de `status` documentado em
    `homecareos.db.models.enums.DocumentoStatus`.
    """

    __tablename__ = "documentos"
    __table_args__ = (Index("ix_documentos_paciente_id_competencia", "paciente_id", "competencia"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: nem sempre dá pra associar paciente/operadora já na ingestão.
    paciente_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pacientes.id"), nullable=True
    )
    operadora_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operadoras.id"), nullable=True
    )
    tipo: Mapped[TipoDocumento] = mapped_column(
        SAEnum(
            TipoDocumento,
            name="documento_tipo",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    arquivo_url: Mapped[str] = mapped_column(String, nullable=False)
    # Competência no formato `YYYY-MM`.
    competencia: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[DocumentoStatus] = mapped_column(
        SAEnum(
            DocumentoStatus,
            name="documento_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=DocumentoStatus.PROCESSANDO,
        index=True,
    )
    # Página de origem quando o documento veio de um PDF multi-página.
    pagina: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
