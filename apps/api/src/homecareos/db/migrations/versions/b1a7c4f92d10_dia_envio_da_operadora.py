"""dia de envio da operadora e campo de origem da pendência

Revision ID: b1a7c4f92d10
Revises: e5c3d5af888e
Create Date: 2026-09-02 21:10:00.000000

Duas colunas que a classificação em buckets de glosa (issue #7) precisa:

`operadoras.dia_envio` é a origem do deadline das pendências — o prazo é esse
dia no mês seguinte à competência do documento. O `10` do server_default é
placeholder; cada operadora tem seu calendário real, que é dado de operação e é
ajustado por UPDATE, sem deploy. Não usa `operadoras.config` (JSONB) de
propósito: aquela coluna é do config de matching de regras, propósito
diferente, e misturar os dois faria o deadline depender de um documento sem
esquema.

`pendencias.campo` é a chave de reconciliação entre as pendências já abertas e
as que uma revalidação propõe. Sem ela o campo violado só existe embutido no
meio da string `descricao`, que não serve como chave — e revalidar duplicaria
pendência ou deixaria órfã a que parou de reprovar. Nasce `nullable=True` e
**sem** `server_default`: pendência anterior a esta feature não tem campo
conhecido, e o `NULL` honesto é melhor que um `''` que finge saber.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1a7c4f92d10"
down_revision: str | None = "e5c3d5af888e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "operadoras",
        sa.Column("dia_envio", sa.Integer(), server_default=sa.text("10"), nullable=False),
    )
    op.create_check_constraint(
        "ck_operadoras_dia_envio", "operadoras", "dia_envio BETWEEN 1 AND 31"
    )
    op.add_column("pendencias", sa.Column("campo", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("pendencias", "campo")
    op.drop_constraint("ck_operadoras_dia_envio", "operadoras", type_="check")
    op.drop_column("operadoras", "dia_envio")
