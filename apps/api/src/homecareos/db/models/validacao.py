"""Model do resultado da aplicação de uma regra sobre um documento."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base
from homecareos.db.models.enums import ResultadoValidacao


class Validacao(Base):
    """Resultado (aprovado/reprovado) da aplicação de uma `Regra` sobre um `Documento`."""

    __tablename__ = "validacoes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    documento_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documentos.id"), nullable=False
    )
    regra_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("regras.id"), nullable=False
    )
    resultado: Mapped[ResultadoValidacao] = mapped_column(
        SAEnum(
            ResultadoValidacao,
            name="validacao_resultado",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    detalhe: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
