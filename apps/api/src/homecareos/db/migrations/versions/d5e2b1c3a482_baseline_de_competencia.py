"""baseline de competência: números de glosa informados à mão

Revision ID: d5e2b1c3a482
Revises: c4f1a0d2b371
Create Date: 2026-09-03 00:00:00.000000

Tabela que guarda o que a operadora **recusou depois do envio** (glosa), a
única medida capaz de sustentar uma comparação antes/depois honesta: o sistema
mede pendência detectada antes do envio, que é outra coisa. Ver a docstring de
`homecareos.reports.metricas`.

São criados **dois** índices únicos, e não um:

- `uq_baselines_competencia_operadora` cobre a chave natural
  `(competencia, operadora_id)` quando há operadora;
- `uq_baselines_competencia_consolidado` é parcial (`operadora_id IS NULL`)
  porque no Postgres dois `NULL` não colidem em índice único — sem ele daria
  para gravar dois consolidados contraditórios para a mesma competência, e o
  `PUT /api/relatorios/baseline` (upsert pela chave natural) passaria a
  atualizar uma linha arbitrária das duas.

`valor_glosado_centavos` é `BigInteger` em centavos, nunca `Float`: dinheiro em
ponto flutuante acumula erro de arredondamento.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e2b1c3a482"
down_revision: str | None = "c4f1a0d2b371"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "baselines_competencia",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("competencia", sa.String(), nullable=False),
        sa.Column("operadora_id", sa.UUID(), nullable=True),
        sa.Column("documentos_enviados", sa.Integer(), nullable=False),
        sa.Column("documentos_glosados", sa.Integer(), nullable=False),
        sa.Column("valor_glosado_centavos", sa.BigInteger(), nullable=True),
        sa.Column("horas_conferencia", sa.Float(), nullable=True),
        sa.Column("fonte", sa.String(), nullable=False),
        sa.Column("observacao", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("documentos_enviados >= 0", name="ck_baselines_enviados_nao_negativo"),
        sa.CheckConstraint("documentos_glosados >= 0", name="ck_baselines_glosados_nao_negativo"),
        sa.CheckConstraint(
            "documentos_glosados <= documentos_enviados",
            name="ck_baselines_glosados_ate_enviados",
        ),
        sa.ForeignKeyConstraint(["operadora_id"], ["operadoras.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_baselines_competencia_operadora",
        "baselines_competencia",
        ["competencia", "operadora_id"],
        unique=True,
    )
    op.create_index(
        "uq_baselines_competencia_consolidado",
        "baselines_competencia",
        ["competencia"],
        unique=True,
        postgresql_where=sa.text("operadora_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_baselines_competencia_consolidado", table_name="baselines_competencia")
    op.drop_index("uq_baselines_competencia_operadora", table_name="baselines_competencia")
    op.drop_table("baselines_competencia")
