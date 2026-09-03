"""tokens de recuperação de senha — autoatendimento por e-mail

Revision ID: d3b7c1e5f240
Revises: c9e2f5a8b104
Create Date: 2026-09-03 00:00:00.000000

Implementa a issue #34: tabela `tokens_recuperacao`, com FK para `usuarios` —
ao contrário de `tentativas_login`, aqui só existe linha para conta que existe.
Pedido de recuperação para e-mail desconhecido não grava nada: o endpoint
responde 204 do mesmo jeito (ver `auth/router.py`), e é o silêncio no banco que
mantém a resposta indistinguível sem precisar inventar linha órfã.

`token_hash` é único e guarda SHA-256, nunca o token — ver
`db/models/token_recuperacao.py`. O índice em `(usuario_id, created_at)` é a
consulta do freio de emissão por hora.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3b7c1e5f240"
down_revision: str | None = "c9e2f5a8b104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tokens_recuperacao",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_tokens_recuperacao_usuario_created_at",
        "tokens_recuperacao",
        ["usuario_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tokens_recuperacao_usuario_created_at", table_name="tokens_recuperacao")
    op.drop_table("tokens_recuperacao")
