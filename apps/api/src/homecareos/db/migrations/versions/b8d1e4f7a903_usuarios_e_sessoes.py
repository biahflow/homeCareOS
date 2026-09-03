"""usuarios, sessoes e a identidade de quem agiu na auditoria

Revision ID: b8d1e4f7a903
Revises: e6f3c2d4b593
Create Date: 2026-09-03 00:00:00.000000

Implementa o ADR 0001 (issue #30): identidade de usuário na API, com sessão de
estado no Postgres.

Duas tabelas novas:

- `usuarios` — `senha_hash` guarda Argon2id, nunca a senha. `email` é único, e
  é o índice único que decide a colisão de cadastro (uma consulta prévia tem
  janela de corrida entre a leitura e a escrita).
- `sessoes` — `token_hash` guarda o SHA-256 do token opaco que viaja no cookie,
  nunca o token: um dump de banco vazado não entrega sessão utilizável. O
  índice em `usuario_id` é o de "revogar todas as sessões desta pessoa".

`usuarios.papel` é `String` e não tipo enum nativo, seguindo `regras.acao` e
`alertas_enviados.tipo`: papel novo não pode exigir migration de tipo enum. O
fechamento da escrita é o enum do pydantic (`homecareos.auth.schema.Papel`).

E duas colunas de identidade referencial, **ambas nullable**:

- `log_conferencia.usuario_id` e `pendencias.responsavel_id`.

Nullable não é frouxidão: linha histórica não tem pessoa, e ação de máquina
(`"api"` da chave de integração, `"sistema"` do dispatcher de extração) também
não. As colunas de texto `usuario` e `responsavel` continuam existindo e
continuam sendo o rótulo legível — nenhuma linha existente é reescrita por esta
migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d1e4f7a903"
down_revision: str | None = "e6f3c2d4b593"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usuarios",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("senha_hash", sa.String(), nullable=False),
        sa.Column("papel", sa.String(), nullable=False),
        sa.Column("ativo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "sessoes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index("ix_sessoes_usuario_id", "sessoes", ["usuario_id"])

    op.add_column("log_conferencia", sa.Column("usuario_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_log_conferencia_usuario_id_usuarios",
        "log_conferencia",
        "usuarios",
        ["usuario_id"],
        ["id"],
    )
    op.add_column("pendencias", sa.Column("responsavel_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_pendencias_responsavel_id_usuarios",
        "pendencias",
        "usuarios",
        ["responsavel_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_pendencias_responsavel_id_usuarios", "pendencias", type_="foreignkey")
    op.drop_column("pendencias", "responsavel_id")
    op.drop_constraint(
        "fk_log_conferencia_usuario_id_usuarios", "log_conferencia", type_="foreignkey"
    )
    op.drop_column("log_conferencia", "usuario_id")
    op.drop_index("ix_sessoes_usuario_id", table_name="sessoes")
    op.drop_table("sessoes")
    op.drop_table("usuarios")
