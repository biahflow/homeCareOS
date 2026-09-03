"""segundo fator por TOTP: segredo, sessão pendente e códigos de recuperação

Revision ID: e1f4a7c92b58
Revises: d3b7c1e5f240
Create Date: 2026-09-03 00:00:00.000000

Implementa a issue #35: MFA por TOTP em app autenticador.

Três colunas em `usuarios` — `mfa_secret`, `mfa_ativado` e `mfa_ultimo_passo`.
`mfa_ultimo_passo` é o maior passo TOTP já aceito da conta e é o **anti-replay**
(ver `auth/mfa.verificar_codigo`): sem ele o mesmo código de seis dígitos
valeria durante toda a janela de tolerância, e quem o interceptasse teria ~90
segundos para reusá-lo.

Uma coluna em `sessoes` — `mfa_pendente`. É ela que faz o segundo fator não ser
decorativo: `auth/sessoes.resolver_sessao` devolve `None` para sessão pendente,
então a sessão criada no primeiro passo do login não abre rota nenhuma de
`/api/*`. `server_default=false` cobre as sessões que já existem: ninguém é
deslogado por esta migration.

E a tabela `codigos_recuperacao_mfa`, com os códigos **hasheados em Argon2id** —
eles são curtos, mas são credencial de login completa (pulam o segundo fator);
ver `db/models/codigo_recuperacao_mfa.py`.

## Limitação declarada, não escondida

`mfa_secret` fica **em claro** no banco. Com um dump, o atacante gera códigos
válidos. Não há KMS neste projeto, e "criptografar" com uma chave guardada no
mesmo `.env` que acompanha o dump seria teatro: quem tem o banco geralmente tem
a configuração. A limitação é declarada, não escondida — ver o README.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f4a7c92b58"
down_revision: str | None = "d3b7c1e5f240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("usuarios", sa.Column("mfa_secret", sa.String(), nullable=True))
    op.add_column(
        "usuarios",
        sa.Column("mfa_ativado", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("usuarios", sa.Column("mfa_ultimo_passo", sa.BigInteger(), nullable=True))

    op.add_column(
        "sessoes",
        sa.Column("mfa_pendente", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.create_table(
        "codigos_recuperacao_mfa",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("codigo_hash", sa.String(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo_hash"),
    )
    op.create_index(
        "ix_codigos_recuperacao_mfa_usuario_id",
        "codigos_recuperacao_mfa",
        ["usuario_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_codigos_recuperacao_mfa_usuario_id", table_name="codigos_recuperacao_mfa")
    op.drop_table("codigos_recuperacao_mfa")
    op.drop_column("sessoes", "mfa_pendente")
    op.drop_column("usuarios", "mfa_ultimo_passo")
    op.drop_column("usuarios", "mfa_ativado")
    op.drop_column("usuarios", "mfa_secret")
