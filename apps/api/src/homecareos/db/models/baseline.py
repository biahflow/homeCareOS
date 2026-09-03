"""Model do baseline de glosa de uma competência — dado informado à mão.

Esta tabela é o **único** lugar do sistema onde entra número de glosa: o que a
operadora recusou depois do envio. É medida diferente da que a conferência
produz (pendência detectada *antes* do envio) e por isso vive separada, sem
nenhum vínculo com `pendencias` — ver a docstring de `homecareos.reports.metricas`
para por que as duas nunca são divididas uma pela outra.

Não é "dado de antes do sistema": é dado de glosa de qualquer competência,
anterior ou posterior à implantação. É isso que permite comparar glosa contra
glosa, a mesma medida nas duas pontas.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class BaselineCompetencia(Base):
    """Números de envio e glosa de uma competência, digitados a partir de um demonstrativo."""

    __tablename__ = "baselines_competencia"
    __table_args__ = (
        CheckConstraint("documentos_enviados >= 0", name="ck_baselines_enviados_nao_negativo"),
        CheckConstraint("documentos_glosados >= 0", name="ck_baselines_glosados_nao_negativo"),
        CheckConstraint(
            "documentos_glosados <= documentos_enviados",
            name="ck_baselines_glosados_ate_enviados",
        ),
        # Dois índices únicos, e não um: no Postgres dois `NULL` não colidem em
        # índice único, então `(competencia, operadora_id)` sozinho deixaria
        # gravar dois consolidados contraditórios para a mesma competência. O
        # índice parcial abaixo é o que fecha esse buraco.
        Index("uq_baselines_competencia_operadora", "competencia", "operadora_id", unique=True),
        Index(
            "uq_baselines_competencia_consolidado",
            "competencia",
            unique=True,
            postgresql_where=text("operadora_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Competência no formato `YYYY-MM`, igual a `documentos.competencia`.
    competencia: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable de propósito: `NULL` é o consolidado de todas as operadoras, que
    # é o número que costuma existir primeiro (o demonstrativo global chega
    # antes da abertura por convênio).
    operadora_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operadoras.id"), nullable=True
    )
    documentos_enviados: Mapped[int] = mapped_column(Integer, nullable=False)
    documentos_glosados: Mapped[int] = mapped_column(Integer, nullable=False)
    # `BigInteger` em centavos, nunca `Float`: dinheiro em ponto flutuante
    # acumula erro de arredondamento, e este é justamente o número que vai
    # parar num slide de ROI.
    valor_glosado_centavos: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Esforço manual gasto na competência, para dimensionar o custo da
    # conferência que hoje é feita à mão.
    horas_conferencia: Mapped[float | None] = mapped_column(Float, nullable=True)
    # De onde o número saiu (planilha da operação, demonstrativo da operadora).
    # Obrigatório: baseline sem procedência não é auditável e não pode
    # sustentar uma comparação antes/depois.
    fonte: Mapped[str] = mapped_column(String, nullable=False)
    observacao: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
