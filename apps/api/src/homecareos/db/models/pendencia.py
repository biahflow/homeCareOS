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
    __table_args__ = (
        Index("ix_pendencias_deadline", "deadline"),
        # A FK para `documentos` não cria índice sozinha no Postgres, e esta é a
        # coluna mais consultada da tabela: o relatório de conferência busca as
        # pendências de uma página inteira (`documento_id IN (...)`), as
        # métricas fazem `EXISTS` por documento e os detectores de alerta
        # juntam `pendencias` a `documentos`. Sem o índice, todas elas caem em
        # varredura sequencial num volume de fechamento de competência.
        Index("ix_pendencias_documento_id", "documento_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    documento_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documentos.id"), nullable=False
    )
    tipo_problema: Mapped[str] = mapped_column(String, nullable=False)
    # Campo do schema de extração que originou a pendência. É a chave (junto com
    # `tipo_problema`) que a revalidação usa para reconciliar o que já está
    # aberto com o que voltou a reprovar — ver
    # `classification.service._reconciliar_pendencias`. Nullable: pendência
    # anterior à issue #7 não tem campo conhecido, e forjar um valor faria a
    # reconciliação casar coisas que não são a mesma.
    campo: Mapped[str | None] = mapped_column(String, nullable=True)
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
