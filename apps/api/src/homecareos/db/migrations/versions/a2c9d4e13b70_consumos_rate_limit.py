"""consumos de rota cara — contador do rate limit por identidade (ADR 0005)

Revision ID: a2c9d4e13b70
Revises: b07495be8cec
Create Date: 2026-09-04 00:00:00.000000

Implementa o ADR 0005 (issue #39): rate limit por identidade do principal, só
nas quatro rotas caras, com o contador no Postgres.

**Uma linha por consumo**, no padrão de `tentativas_login`: a contagem é um
`COUNT` sobre a janela de uma hora, e não um bucket com `UPSERT`. O volume das
quatro rotas limitadas é baixo — elas já falam com storage, provider de IA ou
gateway de WhatsApp —, e uma linha por evento é o que permite auditar um 429
depois ("quem consumiu, o quê, quando").

**Sem FK para `usuarios`**, como `tentativas_login` e pela razão análoga: a
chave do contador é a identidade do principal, e a integração
máquina-a-máquina (`X-API-Key`) não é uma pessoa — ela entra como `maquina:api`.
Uma FK obrigaria a inventar uma linha em `usuarios` para ela.

`recurso` é `String` e **não** tipo enum nativo, seguindo `usuarios.papel`,
`regras.acao` e `alertas_enviados.tipo`. O ADR diz explicitamente que a lista de
rotas limitadas cresce ("se a operação começar a sofrer abuso nelas, o limite se
estende"); com enum nativo, estender custaria `ALTER TYPE` e uma migration só
para isso — e o `downgrade` de um tipo enum é justamente a armadilha que
`e5c3d5af888e` teve de resolver à mão. O fechamento da escrita é o enum do
Python (`homecareos.limites.schema.Recurso`). Consequência: esta migration não
cria nem dropa tipo nenhum, e o `downgrade` é simétrico ao `upgrade`.

Índice composto em `(chave, recurso, created_at)`: é exatamente a consulta do
freio — "quantas linhas desta chave, deste recurso, desde X" — e o
`min(created_at)` da mesma janela, que calcula o `Retry-After`.

**Follow-up conhecido, fora do escopo desta migration:** a tabela cresce a cada
consumo e precisa entrar na política de retenção de `homecareos.retencao`, com
janela mínima respeitando a janela do limite (1h, `limites/protecao.JANELA`) —
a mesma trava que hoje protege `tentativas_login` de ser expurgada dentro da
janela do freio de login.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c9d4e13b70"
down_revision: str | None = "b07495be8cec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consumos_rate_limit",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("chave", sa.String(), nullable=False),
        sa.Column("recurso", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_consumos_rate_limit_chave_recurso_created_at",
        "consumos_rate_limit",
        ["chave", "recurso", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_consumos_rate_limit_chave_recurso_created_at", table_name="consumos_rate_limit"
    )
    op.drop_table("consumos_rate_limit")
