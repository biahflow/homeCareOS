"""Model da operadora (convênio/plano) — dona das regras de conferência e do config de matching."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class Operadora(Base):
    """Convênio/operadora de saúde para o qual o home care presta serviço."""

    __tablename__ = "operadoras"
    __table_args__ = (
        CheckConstraint("dia_envio BETWEEN 1 AND 31", name="ck_operadoras_dia_envio"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nome: Mapped[str] = mapped_column(String, nullable=False)
    codigo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Nullable: o seed inicial (`homecareos.seed`) cadastra operadoras só por
    # nome/código; o config de matching de regras é preenchido depois.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Dia do mês em que a operadora recebe o faturamento; é dele que sai o
    # deadline das pendências (ver `classification.engine.calcular_deadline`).
    # O `10` do server_default é PLACEHOLDER, não o calendário de ninguém: cada
    # operadora tem o seu, que é dado de operação e será acertado linha a linha
    # (UPDATE) sem deploy. Por isso o seed não inventa valores diferentes por
    # operadora — um número errado com cara de certo é pior que um placeholder
    # declarado.
    dia_envio: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
