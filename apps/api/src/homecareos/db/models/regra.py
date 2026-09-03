"""Model de regra de validação de uma operadora, aplicada durante a conferência."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class Regra(Base):
    """Regra de validação de campo, definida por operadora."""

    __tablename__ = "regras"
    __table_args__ = (Index("uq_regras_operadora_codigo", "operadora_id", "codigo", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operadora_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operadoras.id"), nullable=False
    )
    campo: Mapped[str] = mapped_column(String, nullable=False)
    condicao: Mapped[str] = mapped_column(String, nullable=False)
    acao: Mapped[str] = mapped_column(String, nullable=False)
    motivo_glosa: Mapped[str] = mapped_column(String, nullable=False)
    ativo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Chave natural do catálogo de regras (issue #10), ex.: `TISS-EVOL-PACIENTE`.
    # `NULL` para regra criada via `POST /api/regras`, que não vem de catálogo
    # nenhum — e é o que faz o índice único abaixo nunca colidir com ela.
    codigo: Mapped[str | None] = mapped_column(String, nullable=True)
    # De onde a exigência saiu (norma pública ou manual de operadora ainda não
    # verificado). `NULL` pelo mesmo motivo de `codigo`: regra via API não tem
    # fonte de catálogo.
    fonte: Mapped[str | None] = mapped_column(String, nullable=True)
    # `str`, não `SAEnum`, porque a coluna é `String` — mesma escolha já feita
    # para `acao` acima. É o enum `EscopoRegra` (`rules/schema.py`) que fecha a
    # escrita. `server_default` `'operadora'`: a linha pré-existente (criada via
    # API para uma operadora) é, por definição, específica daquela operadora.
    escopo: Mapped[str] = mapped_column(
        String, nullable=False, default="operadora", server_default="operadora"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
