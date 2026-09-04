"""configuração de canais de alerta em banco (ADR 0006, parte 2)

Revision ID: a4d6c8b21f37
Revises: c7b2f0a9e153
Create Date: 2026-09-04 00:00:00.000000

O liga/desliga dos canais de alerta sai do ambiente e vai para o banco, e a
mudança passa a ser auditada (ADR 0006, issue #9). Duas tabelas:

- `canais_alerta` — uma linha por canal, com o estado que quem opera decide e
  o par "quem decidiu / quando". **Não** é uma tabela genérica de chave-valor:
  o ADR descartou essa forma porque sem tipo e sem validação ela vira o
  depósito onde configuração entra sem revisão.
- `auditoria_canais_alerta` — o histórico das mudanças. Tabela **própria**, e
  não `auditoria_usuarios`: lá `alvo_usuario_id` é `NOT NULL` com FK para
  `usuarios`, e registrar "fulano desligou o WhatsApp" obrigaria a inventar um
  alvo fictício, corrompendo o dado que a issue #30 criou. O padrão do projeto
  é uma tabela de auditoria por entidade de domínio (`log_conferencia` para
  documento, `auditoria_usuarios` para usuário).

Nenhum tipo enum nativo é criado aqui (`canal` é `String`, como
`alertas_enviados.canal`), então o `downgrade()` não precisa dropar tipo à mão
— diferente de `e5c3d5af888e`, que cria.

## Por que esta migration INSERE dado, quebrando o padrão do projeto

O padrão da casa para dado inicial é `homecareos.seed`, idempotente com
`ON CONFLICT DO NOTHING` e rodado como ferramenta
(`docker compose run --rm api-seed`). **Aqui ele não basta, e a diferença não é
estilo.**

O seed é um passo separado do `alembic upgrade`, e entre um e outro existe uma
janela em que o código novo já está no ar lendo esta tabela. Para catálogo de
regras essa janela é inofensiva: uma regra que ainda não existe não reprova
documento nenhum, e ninguém perde nada. Para "quais canais avisam a equipe" ela
é o pior desfecho possível — a tabela vazia significa **nenhum canal envia**, a
operação fica em silêncio a partir do deploy, e a ausência de alerta é
indistinguível de "não havia o que alertar". Uma migração de *configuração*, por
definição, não deve mudar comportamento.

Por isso as linhas iniciais nascem aqui, e **espelhando `ALERTAS_CANAIS`**: quem
roda hoje com `ALERTAS_CANAIS=whatsapp` (o default de `Settings`) continua
exatamente como estava, e a troca de fonte fica invisível até alguém mexer na
tela.

## Por que ler configuração de ambiente numa migration

Também não tem precedente aqui, e à primeira vista parece gambiarra. A
alternativa seria fixar `whatsapp` literal — mas isso religaria o WhatsApp de
quem o tivesse desligado e ignoraria quem já tivesse ligado o e-mail, que é
precisamente a mudança de comportamento que esta migration existe para não
causar. **O valor antigo é o único default honesto**, e ele mora em
`ALERTAS_CANAIS`.

A leitura é `Settings()`, e não `os.environ`, pela mesma razão que `env.py` já
usa `get_settings()` para a URL do banco: fora do Compose a variável costuma
viver no `.env`, que `os.environ` não enxerga — e semear um estado diferente do
que a aplicação lia seria reintroduzir, em silêncio, a mudança de comportamento
que se quer evitar.

O parse é feito **aqui**, sobre a lista literal de canais conhecidos, em vez de
importar `alerts.config`: uma migration é histórica e não pode quebrar quando o
código de aplicação evoluir. Canal desconhecido **para a migration** com
mensagem, em vez de virar "todos desligados": um typo em `ALERTAS_CANAIS` que
hoje já derruba a varredura com 422 não pode se transformar em silêncio da
operação, e um deploy que para é visível — um deploy que sobe mudo não é.

Depois desta migration `ALERTAS_CANAIS` deixa de decidir qualquer coisa; ela
permanece em `Settings` exclusivamente como semente daqui. Ver
`alerts/config.canais_habilitados`, o `.env.example` e a seção "Alertas" do
`apps/api/README.md`.

## Índices

`canais_alerta` fica só com a PK e o `unique` de `canal`: são duas linhas, lidas
inteiras a cada varredura, e um índice a mais custaria escrita sem ser usado por
planejador nenhum nesse tamanho.

`auditoria_canais_alerta` cresce e recebe os três da forma de
`auditoria_usuarios`: `canal` (o filtro central da leitura), `usuario_id` (o
filtro por ator — FK não indexa sozinha no Postgres) e `created_at` (a ordenação
padrão da listagem, da qual o expurgo por retenção pega carona).

## Retenção

`auditoria_canais_alerta` entra na política de retenção (`homecareos.retencao`)
como sexta tabela, com **piso de valor de auditoria** e não de freio: nenhum
freio de segurança a consulta. Ver `retencao/janelas.py`.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from homecareos.config import get_settings

# revision identifiers, used by Alembic.
revision: str = "a4d6c8b21f37"
down_revision: str | None = "c7b2f0a9e153"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Os canais que existiam quando esta migration foi escrita. Literal de
# propósito: uma migration é um fato histórico, e importar o enum de
# `alerts.schema` faria o passado mudar junto com o código de hoje.
CANAIS_CONHECIDOS: tuple[str, ...] = ("whatsapp", "email")


def _canais_semeados() -> set[str]:
    """Os canais ligados em `ALERTAS_CANAIS`, validados. Ver a docstring do módulo."""
    ligados: set[str] = set()
    for bruto in get_settings().alertas_canais.split(","):
        nome = bruto.strip()
        if not nome:
            continue
        if nome not in CANAIS_CONHECIDOS:
            raise RuntimeError(
                f"ALERTAS_CANAIS tem o canal desconhecido {nome!r}, e esta migration usa "
                "essa variável como estado inicial da tabela `canais_alerta` (ADR 0006). "
                f"Canais válidos: {', '.join(CANAIS_CONHECIDOS)}. Corrija a variável e "
                "rode a migration de novo — subir com ela errada deixaria a operação sem "
                "aviso nenhum, em silêncio."
            )
        ligados.add(nome)
    return ligados


def upgrade() -> None:
    canais_alerta = op.create_table(
        "canais_alerta",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("canal", sa.String(), nullable=False),
        sa.Column("habilitado", sa.Boolean(), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("atualizado_por", sa.String(), nullable=True),
        sa.Column("atualizado_por_usuario_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["atualizado_por_usuario_id"],
            ["usuarios.id"],
            name="fk_canais_alerta_atualizado_por_usuario_id_usuarios",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canal", name="uq_canais_alerta_canal"),
    )

    # `atualizado_em`/`atualizado_por` ficam nulos: ninguém decidiu isto, foi
    # herdado de `ALERTAS_CANAIS`. Um ator fictício ("migration", "sistema")
    # faria a tela mentir sobre uma decisão que pessoa nenhuma tomou.
    ligados = _canais_semeados()
    op.bulk_insert(
        canais_alerta,
        [
            {"id": uuid.uuid4(), "canal": canal, "habilitado": canal in ligados}
            for canal in CANAIS_CONHECIDOS
        ],
    )

    op.create_table(
        "auditoria_canais_alerta",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usuario", sa.String(), nullable=False),
        sa.Column("usuario_id", sa.UUID(), nullable=True),
        sa.Column("canal", sa.String(), nullable=False),
        sa.Column("habilitado_de", sa.Boolean(), nullable=False),
        sa.Column("habilitado_para", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
            name="fk_auditoria_canais_alerta_usuario_id_usuarios",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auditoria_canais_alerta_canal", "auditoria_canais_alerta", ["canal"])
    op.create_index(
        "ix_auditoria_canais_alerta_usuario_id", "auditoria_canais_alerta", ["usuario_id"]
    )
    op.create_index(
        "ix_auditoria_canais_alerta_created_at", "auditoria_canais_alerta", ["created_at"]
    )


def downgrade() -> None:
    """Volta a fonte do liga/desliga para `ALERTAS_CANAIS`.

    Reversível de verdade **porque a variável não foi removida**: um `.env` que
    ainda a tenha volta a decidir sozinho. O que se perde é o histórico das
    mudanças feitas pela tela — dado de auditoria não tem para onde voltar, e é
    a razão de o downgrade de uma tabela de auditoria ser sempre uma decisão
    consciente, não um passo de rotina.
    """
    op.drop_index("ix_auditoria_canais_alerta_created_at", table_name="auditoria_canais_alerta")
    op.drop_index("ix_auditoria_canais_alerta_usuario_id", table_name="auditoria_canais_alerta")
    op.drop_index("ix_auditoria_canais_alerta_canal", table_name="auditoria_canais_alerta")
    op.drop_table("auditoria_canais_alerta")
    op.drop_table("canais_alerta")
