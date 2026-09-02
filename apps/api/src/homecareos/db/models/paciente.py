"""Model do paciente em acompanhamento home care, vinculado a uma operadora."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base
from homecareos.db.models.enums import Modalidade


class Paciente(Base):
    """Paciente acompanhado pelo home care, vinculado a uma operadora."""

    __tablename__ = "pacientes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nome: Mapped[str] = mapped_column(String, nullable=False)
    operadora_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operadoras.id"), nullable=False
    )
    # Nullable: nem sempre o PAD (Plano de Assistência Domiciliar) já foi
    # emitido pela operadora quando o paciente é cadastrado.
    data_vencimento_pad: Mapped[date | None] = mapped_column(Date, nullable=True)
    status_pad: Mapped[str | None] = mapped_column(String, nullable=True)
    modalidade: Mapped[Modalidade] = mapped_column(
        SAEnum(
            Modalidade,
            name="paciente_modalidade",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
