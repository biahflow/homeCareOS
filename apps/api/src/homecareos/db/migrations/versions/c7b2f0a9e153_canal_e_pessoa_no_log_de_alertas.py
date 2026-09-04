"""canal e pessoa no log de alertas (ADR 0006)

Revision ID: c7b2f0a9e153
Revises: a2c9d4e13b70
Create Date: 2026-09-04 00:00:00.000000

O log de alertas ganha as duas colunas que um segundo canal exige (ADR 0006,
issue #9). Sem elas o anti-bombardeio quebra em **silêncio**: `destinatario`
acumulava dois papéis — identificar o canal (era sempre um telefone) e
identificar a pessoa —, e com dois canais o telefone e o e-mail de alguém viram
destinatários não relacionados. O efeito não é uma exceção: é o teto de
mensagens por hora **dobrar sem ninguém pedir**.

- `canal` (`String`, NOT NULL) — por onde a mensagem saiu. `String` e não tipo
  enum nativo, seguindo `alertas_enviados.tipo`, `usuarios.papel` e
  `regras.acao`: um canal novo não deve custar `ALTER TYPE` e uma migration só
  para isso, e o `downgrade` de um tipo enum é a armadilha que `e5c3d5af888e`
  teve de resolver à mão. O fechamento da escrita é `alerts.schema.Canal`.
- `usuario_id` (`UUID`, NULL, FK para `usuarios`) — de quem é o endereço,
  quando o sistema sabe. É a chave nova do rate limit. Fica `NULL` no telefone
  avulso de `ALERTAS_DESTINATARIOS`, que não tem vínculo com pessoa nenhuma
  porque **não há telefone em `usuarios`** — a assimetria é do dado que existe,
  e o ADR a declara em vez de fingir simetria.

## As linhas que já existem

São **todas de WhatsApp**: era o único canal. Por isso `canal` entra nullable,
é preenchida com `'whatsapp'` e só então vira NOT NULL — o histórico continua
coerente e legível, e nenhuma linha antiga fica com canal desconhecido.
`usuario_id` fica `NULL` nelas, o que também é a verdade: eram telefones do
`.env`, sem pessoa associada.

**Sem `server_default` em `canal`** de propósito. Um default de servidor
resolveria o backfill em uma linha, mas ficaria de herança: uma escrita futura
que esquecesse o canal herdaria `whatsapp` em silêncio, e o log passaria a
mentir sobre por onde a mensagem saiu. O backfill é único; o risco de herdar é
para sempre.

## Índice

`ix_alertas_usuario_created_at` é a consulta nova do rate limit — "quantas
mensagens esta PESSOA recebeu desde X". Sem ele, a defesa que roda a cada
alerta de cada varredura (e a varredura é de minuto em minuto no cron) viraria
seq scan numa tabela que só cresce.

Os dois índices que já existem continuam servindo: `ix_alertas_tipo_chave_created_at`
é o cooldown, que o ADR mantém por destinatário (dois canais são dois
endereços, e faz sentido o mesmo aviso sair nos dois), e
`ix_alertas_destinatario_created_at` é o rate limit quando a pessoa **não** é
conhecida, que continua sendo o caso de todo telefone do `.env`.

## Retenção

`alertas_enviados` está na política de retenção (`homecareos.retencao`), com
piso derivado do cooldown (`ALERTAS_COOLDOWN_HORAS`) e da janela do rate limit
(`JANELA_RATE_LIMIT`, 1h). Nenhuma das duas janelas muda aqui, e as linhas que
o expurgo apaga continuam sendo as mesmas (`created_at < corte`): a trava de
`retencao/janelas.pisos_alertas_enviados` segue correta sem alteração.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7b2f0a9e153"
down_revision: str | None = "a2c9d4e13b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Nome que o Postgres daria a esta FK sozinho (`<tabela>_<coluna>_fkey`), o
# mesmo padrão de `alertas_enviados_documento_id_fkey`. Explicitá-lo é o que
# torna o `downgrade` simétrico sem depender de descobrir o nome gerado.
FK_USUARIO = "alertas_enviados_usuario_id_fkey"


def upgrade() -> None:
    op.add_column("alertas_enviados", sa.Column("canal", sa.String(), nullable=True))
    # Todas as linhas anteriores a esta migration são de WhatsApp — ver a
    # docstring. O `where` deixa o backfill idempotente.
    op.execute("update alertas_enviados set canal = 'whatsapp' where canal is null")
    op.alter_column("alertas_enviados", "canal", nullable=False)

    op.add_column("alertas_enviados", sa.Column("usuario_id", sa.UUID(), nullable=True))
    op.create_foreign_key(FK_USUARIO, "alertas_enviados", "usuarios", ["usuario_id"], ["id"])
    op.create_index(
        "ix_alertas_usuario_created_at",
        "alertas_enviados",
        ["usuario_id", "created_at"],
    )


def downgrade() -> None:
    """Volta ao log de um canal só.

    As linhas continuam coerentes: o que se perde é a informação de canal (que
    era `whatsapp` em todas as linhas anteriores a esta migration) e o vínculo
    com a pessoa. `destinatario`, `mensagem`, `status` e `created_at` — que é o
    que o log existe para guardar — ficam intactos, e o rate limit volta a
    contar por endereço, que é o comportamento que o índice
    `ix_alertas_destinatario_created_at` sempre serviu.
    """
    op.drop_index("ix_alertas_usuario_created_at", table_name="alertas_enviados")
    op.drop_constraint(FK_USUARIO, "alertas_enviados", type_="foreignkey")
    op.drop_column("alertas_enviados", "usuario_id")
    op.drop_column("alertas_enviados", "canal")
