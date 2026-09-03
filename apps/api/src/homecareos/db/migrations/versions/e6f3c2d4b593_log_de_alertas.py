"""log de alertas de WhatsApp enviados

Revision ID: e6f3c2d4b593
Revises: f7a4b8c9d012
Create Date: 2026-09-03 00:00:00.000000

Tabela que registra toda tentativa de notificação (issue #9): o texto enviado,
para quem, e no que deu — `enviado`, `falha` ou `suprimido`.

`tipo` e `status` são `String`, não tipo enum nativo do Postgres, seguindo o
que `regras.acao` já faz: um tipo novo de alerta não pode exigir migration de
tipo enum. O fechamento da escrita é o enum do pydantic
(`homecareos.alerts.schema`).

Os dois índices são as consultas da política anti-bombardeio, que rodam a cada
alerta de cada varredura (a varredura é de minuto em minuto no cron):

- `ix_alertas_destinatario_created_at` — quantos envios este número recebeu na
  última hora (rate limit);
- `ix_alertas_tipo_chave_created_at` — já houve envio deste mesmo assunto para
  este destinatário dentro da janela de cooldown.

`mensagem` guarda o texto enviado, que inclui nome de paciente. É decisão
consciente de auditabilidade, justificada na docstring de
`homecareos.db.models.alerta`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f3c2d4b593"
down_revision: str | None = "f7a4b8c9d012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alertas_enviados",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tipo", sa.String(), nullable=False),
        sa.Column("chave", sa.String(), nullable=False),
        sa.Column("destinatario", sa.String(), nullable=False),
        sa.Column("mensagem", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("detalhe", sa.String(), nullable=True),
        sa.Column("documento_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["documento_id"], ["documentos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_alertas_destinatario_created_at",
        "alertas_enviados",
        ["destinatario", "created_at"],
    )
    op.create_index(
        "ix_alertas_tipo_chave_created_at",
        "alertas_enviados",
        ["tipo", "chave", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_alertas_tipo_chave_created_at", table_name="alertas_enviados")
    op.drop_index("ix_alertas_destinatario_created_at", table_name="alertas_enviados")
    op.drop_table("alertas_enviados")
