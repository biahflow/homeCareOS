"""cifra do segredo TOTP em repouso (ADR 0008)

Revision ID: f2b9d6e04a17
Revises: a4d6c8b21f37
Create Date: 2026-09-05 00:00:00.000000

`usuarios.mfa_secret` guardava o segredo TOTP em claro desde `e1f4a7c92b58`.
Esta migration reescreve os segredos existentes como token Fernet, com as
chaves de `MFA_SECRET_KEYS` — a primeira cifra, todas decifram (ADR 0008,
`db/cifra.py`).

**Nenhuma coluna muda de tipo.** O token Fernet é base64url, então cabe na
`String` que já existe; trocar para `LargeBinary` reescreveria a tabela sem
ganho nenhum. O que muda é o conteúdo das linhas.

## Esta migration precisa da chave, e falha alto sem ela

Há segredo para cifrar e `MFA_SECRET_KEYS` vazio: **para**, dizendo o que
configurar. Pular em silêncio deixaria linha em claro num banco que o código
novo trata como cifrada — a coluna ficaria metade cifrada e metade não, sem
ninguém saber quais, e cada pessoa descobriria pelo login que parou de
funcionar.

Não há segredo nenhum para cifrar (banco novo, ninguém ativou MFA): roda sem
chave, sem reclamar. É o caso do CI e de todo ambiente que ainda não usa o
segundo fator, e exigir chave aí seria cobrar configuração por um dado que não
existe.

## O `downgrade()` decifra de volta, e é obrigatório que decifre

Um downgrade que deixasse o token Fernet na coluna devolveria o banco ao
esquema antigo com conteúdo ilegível: o código anterior trataria aquele texto
como segredo base32 válido, e **nenhum código TOTP jamais bateria** — para
todo mundo, em silêncio, sem erro em log nenhum. O rollback ficaria pior que o
problema que ele desfaz.

Por isso o downgrade também exige a chave quando há o que decifrar, e para
quando encontra token que nenhuma chave abre: deixar a linha como está seria
exatamente o desfecho descrito acima.

## Por que a cifra é montada aqui, e não importada de `db/cifra.py`

Mesma razão pela qual `a4d6c8b21f37` faz o parse de `ALERTAS_CANAIS` sobre uma
lista literal em vez de importar `alerts.config`: uma migration é um fato
histórico e não pode mudar de comportamento quando o código de aplicação
evoluir. Se um dia a cifra da aplicação virar envelope de KMS, esta migration
precisa continuar fazendo o que fez no dia em que rodou.

O que ela compartilha com a aplicação é só a **fonte da configuração**
(`get_settings().mfa_secret_keys`), lida com `Settings` e não com `os.environ`
pelo mesmo motivo que `env.py` já usa `get_settings()` para a URL do banco:
fora do Compose a variável costuma viver no `.env`, que `os.environ` não
enxerga.

## Idempotência

Os dois sentidos verificam o estado antes de reescrever: `upgrade()` pula o que
já decifra (já está cifrado) e `downgrade()` pula o que não tem forma de token
Fernet (já está em claro). Rodar de novo depois de uma interrupção no meio não
corrompe nada, e uma tabela em estado misto converge.

## Não atômica em relação ao processo, atômica em relação ao banco

O `UPDATE` de todas as linhas entra na transação que o Alembic já abre para a
migration, então não existe estado parcial visível. O que não existe é
paralelismo: a operação é linear no número de contas **com MFA ativo**, que é
um subconjunto pequeno de `usuarios` — não há tabela grande envolvida aqui.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from homecareos.config import get_settings

# revision identifiers, used by Alembic.
revision: str = "f2b9d6e04a17"
down_revision: str | None = "a4d6c8b21f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Prefixo de todo token Fernet (versão 0x80 + timestamp, em base64url). Usado
# só pelo `downgrade()`, para separar "token que nenhuma chave abre" — que é
# erro e para a migration — de "segredo que já está em claro" — que é linha
# nunca cifrada e só precisa ser deixada em paz.
PREFIXO_FERNET = "gAAAAA"

# A nota sobre ONDE a variável precisa estar não é excesso de zelo: `Settings`
# lê o `.env` do diretório de trabalho, e o `.env` deste projeto fica na RAIZ do
# repositório, enquanto o alembic roda de `apps/api`. Rodar a migration à mão
# sem exportar a variável cai exatamente nesta mensagem com o `.env` já
# preenchido, e a instrução "configure o .env" sozinha mandaria a pessoa
# reconfigurar o que já estava certo.
_INSTRUCAO_DA_CHAVE = (
    "Configure MFA_SECRET_KEYS (lista separada por vírgula; a primeira cifra, todas "
    'decifram). Gere uma chave com `python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"`. Pelo Compose, basta a variável no `.env` da '
    "raiz (`docker compose run --rm api-migrate`); rodando o alembic à mão de `apps/api` o "
    "`.env` da raiz NÃO é lido, então exporte a variável: "
    "`MFA_SECRET_KEYS=... uv run alembic upgrade head`."
)


def _cifrador() -> MultiFernet | None:
    """`MultiFernet` de `MFA_SECRET_KEYS`, ou `None` se não houver chave."""
    chaves = [bruta.strip() for bruta in get_settings().mfa_secret_keys.split(",")]
    presentes = [chave for chave in chaves if chave]
    if not presentes:
        return None
    try:
        return MultiFernet([Fernet(chave) for chave in presentes])
    except (ValueError, TypeError) as exc:
        # Nunca ecoa a chave: uma migration que falha costuma ter a saída colada
        # inteira num ticket.
        raise RuntimeError(f"MFA_SECRET_KEYS tem chave inválida. {_INSTRUCAO_DA_CHAVE}") from exc


def _segredos(conexao: sa.Connection) -> list[tuple[uuid.UUID, str]]:
    linhas = conexao.execute(
        sa.text("select id, mfa_secret from usuarios where mfa_secret is not null")
    ).all()
    return [(linha.id, linha.mfa_secret) for linha in linhas]


def _gravar(conexao: sa.Connection, usuario_id: uuid.UUID, valor: str) -> None:
    conexao.execute(
        sa.text("update usuarios set mfa_secret = :valor where id = :id"),
        {"valor": valor, "id": usuario_id},
    )


def upgrade() -> None:
    conexao = op.get_bind()
    segredos = _segredos(conexao)
    if not segredos:
        # Banco novo, ou ninguém ativou o segundo fator: não há dado para
        # proteger, e exigir chave aqui só travaria um deploy sem motivo.
        return

    cifrador = _cifrador()
    if cifrador is None:
        raise RuntimeError(
            f"{len(segredos)} conta(s) têm segredo TOTP em claro em `usuarios.mfa_secret` e "
            "não há chave para cifrá-lo (ADR 0008). Esta migration NÃO pula essas linhas: "
            "metade cifrada e metade em claro é um banco que o código novo lê errado, e cada "
            f"pessoa descobriria pelo login que parou de funcionar. {_INSTRUCAO_DA_CHAVE} "
            "Guarde a chave em backup SEPARADO do banco — sem ela o segundo fator de quem já "
            "o tem ativo fica ilegível, e a saída passa a ser o código de recuperação."
        )

    for usuario_id, valor in segredos:
        try:
            cifrador.decrypt(valor.encode())
        except InvalidToken:
            _gravar(conexao, usuario_id, cifrador.encrypt(valor.encode()).decode())
        # Decifrou: a linha já está cifrada por uma execução anterior. Recifrar
        # não estragaria nada, mas gastaria escrita à toa.


def downgrade() -> None:
    conexao = op.get_bind()
    segredos = _segredos(conexao)
    if not segredos:
        return

    cifrador = _cifrador()
    if cifrador is None:
        raise RuntimeError(
            f"{len(segredos)} conta(s) têm segredo TOTP cifrado em `usuarios.mfa_secret` e "
            "não há chave para decifrá-lo (ADR 0008). Voltar o esquema deixando o token "
            "Fernet na coluna seria pior que não voltar: o código antigo o trataria como "
            "segredo base32 válido e NENHUM código TOTP bateria, para todo mundo, sem erro "
            f"em log nenhum. {_INSTRUCAO_DA_CHAVE}"
        )

    for usuario_id, valor in segredos:
        try:
            _gravar(conexao, usuario_id, cifrador.decrypt(valor.encode()).decode())
        except InvalidToken as exc:
            if not valor.startswith(PREFIXO_FERNET):
                # Segredo que nunca foi cifrado (linha criada entre o upgrade e
                # o downgrade, por exemplo). Já está no formato que o downgrade
                # quer produzir: deixar em paz é o certo.
                continue
            raise RuntimeError(
                f"o segredo TOTP da conta {usuario_id} tem forma de token Fernet e nenhuma "
                "chave de MFA_SECRET_KEYS o abre — provavelmente a chave que o cifrou saiu "
                "da lista. Acrescente-a (todas as chaves da lista decifram) e rode o "
                "downgrade de novo. Deixar a linha como está devolveria o banco ao esquema "
                "antigo com um segredo ilegível, e o login por TOTP dessa pessoa nunca mais "
                "funcionaria."
            ) from exc
