"""auditoria administrativa de usuários

Revision ID: b07495be8cec
Revises: e1f4a7c92b58
Create Date: 2026-09-04 00:00:00.000000

Implementa a issue #30: fecha o ponto que o ADR 0004 deixou explicitamente em
aberto — "não há auditoria de administração de usuário". Uma tabela nova,
`auditoria_usuarios`, append-only, registrando quem fez, em quem, o que
mudou (de que valor para que valor) e quando, para `POST`/`PATCH
/api/usuarios`.

**Uma linha por evento**, não uma por campo alterado — a coluna `mudancas`
(JSONB) carrega o diff inteiro. Ver a docstring de
`db/models/auditoria_usuario.py` para a decisão completa, inclusive por que
`acao` é `String` (como `log_conferencia.acao`) e não um tipo enum nativo do
Postgres: esta migration não cria tipo enum nenhum, e por isso o `downgrade()`
não precisa dropar nenhum à mão (diferente de `e5c3d5af888e`, que cria).

`usuario_id` (o ator) é **nullable**, pelo mesmo motivo de
`log_conferencia.usuario_id`: `X-API-Key` age sem "si mesmo"
(`auth/dependencies.py`). `alvo_usuario_id` (quem sofreu a ação) é `NOT NULL`
— toda linha desta tabela nasce de uma ação sobre um `Usuario` concreto.

Dois índices nomeados à mão, porque FK não indexa sozinha no Postgres e as
duas colunas são o filtro/ordenação da leitura paginada
(`GET /api/usuarios/auditoria`): `alvo_usuario_id` (filtro obrigatório) e
`created_at` (ordenação padrão, mais recente primeiro). Um terceiro em
`usuario_id` cobre o filtro opcional por ator, pela mesma razão de
`ix_sessoes_usuario_id`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b07495be8cec"
down_revision: str | None = "e1f4a7c92b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auditoria_usuarios",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usuario", sa.String(), nullable=False),
        sa.Column("usuario_id", sa.UUID(), nullable=True),
        sa.Column("alvo_usuario_id", sa.UUID(), nullable=False),
        sa.Column("alvo_email", sa.String(), nullable=False),
        sa.Column("acao", sa.String(), nullable=False),
        sa.Column("mudancas", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"], ["usuarios.id"], name="fk_auditoria_usuarios_usuario_id_usuarios"
        ),
        sa.ForeignKeyConstraint(
            ["alvo_usuario_id"],
            ["usuarios.id"],
            name="fk_auditoria_usuarios_alvo_usuario_id_usuarios",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auditoria_usuarios_alvo_usuario_id", "auditoria_usuarios", ["alvo_usuario_id"]
    )
    op.create_index("ix_auditoria_usuarios_usuario_id", "auditoria_usuarios", ["usuario_id"])
    op.create_index("ix_auditoria_usuarios_created_at", "auditoria_usuarios", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_auditoria_usuarios_created_at", table_name="auditoria_usuarios")
    op.drop_index("ix_auditoria_usuarios_usuario_id", table_name="auditoria_usuarios")
    op.drop_index("ix_auditoria_usuarios_alvo_usuario_id", table_name="auditoria_usuarios")
    op.drop_table("auditoria_usuarios")
