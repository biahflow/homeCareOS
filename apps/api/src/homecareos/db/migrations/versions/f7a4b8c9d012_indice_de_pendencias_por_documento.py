"""índice de pendências por documento

Revision ID: f7a4b8c9d012
Revises: d5e2b1c3a482
Create Date: 2026-09-03 00:00:00.000000

`pendencias.documento_id` é a coluna mais consultada da tabela e não tinha
índice: no Postgres, declarar uma chave estrangeira **não** cria índice do lado
que referencia. Enquanto só existia `GET /api/pendencias` (que filtra por
status e deadline) isso não aparecia; a issue #8 mudou o quadro, e agora dois
caminhos quentes batem nessa coluna:

- o relatório de conferência busca as pendências de uma página inteira de
  documentos de uma vez (`documento_id IN (...)`), e ainda usa uma subconsulta
  correlacionada por linha para o menor deadline em aberto;
- as métricas marcam cada documento com um `EXISTS` sobre `pendencias`.

Os detectores de alerta da issue #9, que vem logo em seguida, juntam as duas
tabelas e se beneficiam do mesmo índice.

Sem o índice, cada um deles vira varredura sequencial da tabela — justamente no
fechamento de competência, que é quando ela está maior e quando o relatório
precisa responder.

Índice puro, sem mudança de coluna: é reversível de verdade, e o `downgrade`
não perde dado nenhum.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a4b8c9d012"
down_revision: str | None = "d5e2b1c3a482"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_pendencias_documento_id", "pendencias", ["documento_id"])


def downgrade() -> None:
    op.drop_index("ix_pendencias_documento_id", table_name="pendencias")
