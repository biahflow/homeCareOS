"""catálogo de regras: código, fonte e escopo

Revision ID: c4f1a0d2b371
Revises: b1a7c4f92d10
Create Date: 2026-09-03 00:00:00.000000

Três colunas que a issue #10 (seed da biblioteca inicial de regras) precisa:

`codigo` é a chave natural do catálogo (`TISS-EVOL-PACIENTE`,
`AMIL-AD-COREN-FORMATO`) e o que torna o seed idempotente via
`ON CONFLICT (operadora_id, codigo) DO NOTHING`. Nasce `nullable=True` porque
regra criada por `POST /api/regras` não vem de catálogo nenhum — e no Postgres
`NULL` não colide em índice único, então regra de API nunca conflita com regra
de catálogo nem entre si.

`fonte` é o critério de aceite de auditoria da issue #10: de onde a exigência
saiu. `nullable=True` pelo mesmo motivo (regra criada via API não tem fonte).

`escopo` separa genérica TISS de específica de operadora. `server_default`
`'operadora'` porque a linha pré-existente (criada via API para uma operadora)
é, por definição, específica daquela operadora.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f1a0d2b371"
down_revision: str | None = "b1a7c4f92d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("regras", sa.Column("codigo", sa.String(), nullable=True))
    op.add_column("regras", sa.Column("fonte", sa.String(), nullable=True))
    op.add_column(
        "regras",
        sa.Column("escopo", sa.String(), server_default=sa.text("'operadora'"), nullable=False),
    )
    op.create_index("uq_regras_operadora_codigo", "regras", ["operadora_id", "codigo"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_regras_operadora_codigo", table_name="regras")
    op.drop_column("regras", "escopo")
    op.drop_column("regras", "fonte")
    op.drop_column("regras", "codigo")
