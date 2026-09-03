"""tentativas de login — freio para força bruta contra POST /api/auth/login

Revision ID: c9e2f5a8b104
Revises: b8d1e4f7a903
Create Date: 2026-09-03 00:00:00.000000

Implementa a issue #33: tabela `tentativas_login`, sem FK para `usuarios` — ela
registra a *string tentada*, não um usuário, e metade das linhas úteis é de
e-mail que não existe (a FK tornaria impossível gravá-las, que é justamente o
registro que fecha a enumeração de usuário — ver `auth/protecao.py` e
`db/models/tentativa_login.py`).

Índices em `(email_tentado, created_at)` e `(ip, created_at)`: são exatamente
as duas consultas de janela usadas para decidir atraso progressivo, trava de
IP e trava de conta.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e2f5a8b104"
down_revision: str | None = "b8d1e4f7a903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tentativas_login",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email_tentado", sa.String(), nullable=False),
        sa.Column("ip", sa.String(), nullable=False),
        sa.Column("sucesso", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tentativas_login_email_created_at",
        "tentativas_login",
        ["email_tentado", "created_at"],
    )
    op.create_index(
        "ix_tentativas_login_ip_created_at",
        "tentativas_login",
        ["ip", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tentativas_login_ip_created_at", table_name="tentativas_login")
    op.drop_index("ix_tentativas_login_email_created_at", table_name="tentativas_login")
    op.drop_table("tentativas_login")
