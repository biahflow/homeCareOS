"""Cifra do segredo TOTP em repouso, na fronteira do banco (ADR 0008).

`usuarios.mfa_secret` guardava o segredo TOTP **em claro**. Com um dump, o
atacante gera códigos válidos e passa pelo segundo fator de qualquer pessoa.
Este módulo fecha esse vetor cifrando o valor no caminho para a coluna e
decifrando na volta, com a chave provisionada **separada** do `DATABASE_URL`.

## O que isto protege, e o que não protege

Protege o vetor comum, que é o que quase sempre acontece de verdade: **backup
vazado, réplica de leitura, acesso de DBA, dump obtido por injection**. Nesses
casos o atacante tem o conteúdo do banco e não tem `MFA_SECRET_KEYS`, e o
segredo é um token opaco.

**Não protege host inteiro comprometido.** Quem executa código no servidor da
API lê a variável de ambiente e o banco juntos — e nesse cenário já tem coisa
pior à mão que gerar TOTP. É redução real de superfície, **não é cofre**; o
cofre é KMS/HSM, registrado no ADR 0008 como o passo seguinte.

## Por que `TypeDecorator`, e não cifra no router

Os pontos de uso em `auth/router.py` tratam `mfa_secret` como presença
(`is None` / `is not None`), atribuição (`= segredo`, `= None`) e leitura
(passada a `mfa.verificar_codigo`). Com o decorator **nenhuma dessas linhas
muda**, e não sobra caminho para alguém esquecer de cifrar numa escrita nova:
quem grava na coluna passa por aqui, sempre.

O segredo continua base32 em memória e em trânsito — é o que a pessoa cadastra
no app autenticador, e é do que `mfa.uri_otpauth` deriva o QR code. A cifra é
**em repouso**; o trânsito já é TLS mais sessão.

## Por que `MultiFernet`, e não `Fernet`

`MFA_SECRET_KEYS` é uma lista separada por vírgula: **a primeira cifra, todas
decifram**. É a mesma forma de `API_KEYS`, e pela mesma razão — sem ela, trocar
a chave exigiria downtime e um script de emergência para reescrever a coluna.
Com ela, rotacionar é acrescentar a chave nova na frente, e o segredo cifrado
pela antiga continua sendo lido.

`Fernet` é AES-128-CBC com HMAC-SHA256 e timestamp: cifra **autenticada**, com
modo e IV fechados pela construção. Um token adulterado não decifra — ele falha,
em vez de virar um segredo diferente e silencioso.

## Perder a chave

Perder `MFA_SECRET_KEYS` torna ilegível o segundo fator de quem o tem ativo. A
saída existe e é o **código de recuperação** (`codigos_recuperacao_mfa`,
hasheado em Argon2id e independente do segredo): por isso a leitura de um
segredo indecifrável degrada para `None` em vez de levantar — com `None`,
`POST /api/auth/mfa/verificar` pula o TOTP e cai no código de recuperação, que
continua logando a pessoa. A escrita faz o oposto e **levanta**: gravar em
claro por falta de chave seria desfazer em silêncio a proteção inteira.

A consequência operacional está declarada no ADR: a chave passa a ser material
de backup tão crítico quanto o banco.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import String
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator

from homecareos.config import get_settings

logger = logging.getLogger(__name__)

# Como todo token Fernet começa: versão `0x80` seguida do timestamp de 8 bytes,
# em base64url. Serve só de heurística legível — para distinguir "isto é um
# valor cifrado que não abre com as chaves de agora" de "isto é base32 em claro
# que a migration não alcançou". Quem decide é sempre a tentativa de decifrar;
# este prefixo só melhora a mensagem de erro.
PREFIXO_FERNET = "gAAAAA"

MENSAGEM_SEM_CHAVE = (
    "MFA_SECRET_KEYS está vazio: não há chave para cifrar o segredo TOTP em repouso. "
    "Gere uma com "
    '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` '
    "e configure MFA_SECRET_KEYS no .env. Guarde a chave em backup separado do banco — "
    "sem ela, o segredo de quem já tem MFA ativo fica ilegível."
)


def _chaves(csv: str) -> tuple[str, ...]:
    """As chaves da CSV, na ordem — a primeira é a que cifra."""
    return tuple(bruta.strip() for bruta in csv.split(",") if bruta.strip())


@lru_cache(maxsize=8)
def cifrador_de(csv: str) -> MultiFernet | None:
    """`MultiFernet` das chaves da CSV, ou `None` quando não há nenhuma.

    O cache é pela **string de configuração**, e não global: trocar a
    configuração (em teste, ou num processo que releia o ambiente) devolve um
    cifrador diferente, sem ninguém precisar lembrar de invalidar nada. O teto
    de 8 entradas existe para o cache não virar um lugar onde chave de cifra se
    acumula sem fim na memória do processo.

    Chave presente e malformada **levanta**, e é deliberado: um typo em
    `MFA_SECRET_KEYS` que virasse "sem chave" desligaria a cifra em silêncio,
    que é o desfecho que este módulo existe para não ter. É o mesmo raciocínio
    que a migration `a4d6c8b21f37` aplica a um canal desconhecido em
    `ALERTAS_CANAIS`.
    """
    chaves = _chaves(csv)
    if not chaves:
        return None
    montadas: list[Fernet] = []
    for posicao, chave in enumerate(chaves, start=1):
        try:
            montadas.append(Fernet(chave))
        except (ValueError, TypeError) as exc:
            # A mensagem nunca ecoa a chave — nem a inválida: `.env` colado em
            # ticket de suporte é como segredo vaza. Só a posição na lista.
            raise RuntimeError(
                f"MFA_SECRET_KEYS tem chave inválida na posição {posicao}: cada item precisa "
                "ser uma chave Fernet (32 bytes em base64url), gerada com "
                '`python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"`. A aplicação recusa tratar chave '
                "malformada como 'sem chave' — isso desligaria a cifra do segredo TOTP em "
                "silêncio."
            ) from exc
    return MultiFernet(montadas)


def cifrador() -> MultiFernet | None:
    """O cifrador em vigor, ou `None` quando `MFA_SECRET_KEYS` está vazio."""
    return cifrador_de(get_settings().mfa_secret_keys)


def cifra_disponivel() -> bool:
    """Há chave configurada para cifrar segredo novo?

    Consultada por `POST /api/auth/mfa/iniciar` **antes** de gerar segredo: sem
    chave, a rota recusa (503) em vez de gravar em claro. Um sistema que degrada
    silenciosamente para texto claro é pior que um que recusa.
    """
    return cifrador() is not None


class SegredoCifrado(TypeDecorator[str]):
    """`String` que sai cifrada para o banco e volta decifrada para o código.

    `None` continua `None` nos dois sentidos: é ele que sinaliza "MFA não
    iniciado" em `auth/router.py`, e traduzi-lo para qualquer outra coisa
    mudaria o significado da coluna.

    O token Fernet é guardado como `str` ASCII na **coluna `String` que já
    existe** — `Fernet.encrypt` devolve `bytes`, mas eles são base64url por
    construção. Guardar assim evita migration de tipo de coluna (`String` para
    `LargeBinary`), que reescreveria a tabela inteira, e mantém o valor legível
    em `psql` como o token opaco que ele é.

    A cifra **não é determinística** (IV e timestamp novos a cada `encrypt`), e
    isso é correto: comparar segredo TOTP por igualdade em SQL não é um caso de
    uso — nenhuma consulta do projeto faz `where mfa_secret = ...`, só
    `IS NULL` / `IS NOT NULL`, que o Postgres resolve sem olhar o conteúdo.
    """

    impl = String
    # O tipo não depende de parâmetro de instância nenhum, então o SQLAlchemy
    # pode cachear a compilação das queries que o usam.
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """Cifra na ida. Sem chave configurada, **levanta** em vez de gravar em claro."""
        if value is None:
            return None
        atual = cifrador()
        if atual is None:
            raise RuntimeError(MENSAGEM_SEM_CHAVE)
        return atual.encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        """Decifra na volta. O que não abre vira `None`, com erro no log.

        Degradar para `None` — em vez de levantar — é o que preserva o caminho
        do código de recuperação quando a chave se perde ou é rotacionada errado:
        `/mfa/verificar` só tenta o TOTP quando há segredo, e cai no código de
        recuperação quando não há. Levantar aqui derrubaria com 500 até esse
        último caminho de volta, que é justamente a saída de emergência.

        O valor nunca é logado, nem truncado: mesmo ilegível para nós, ele é o
        segundo fator de alguém.
        """
        if value is None:
            return None
        atual = cifrador()
        if atual is None:
            logger.error(
                "usuarios.mfa_secret não pôde ser decifrado: MFA_SECRET_KEYS está vazio. "
                "O segundo fator desta conta fica indisponível e o login dela depende do "
                "código de recuperação. %s",
                MENSAGEM_SEM_CHAVE,
            )
            return None
        try:
            return atual.decrypt(value.encode()).decode()
        except InvalidToken:
            parece_cifrado = value.startswith(PREFIXO_FERNET)
            logger.error(
                "usuarios.mfa_secret não pôde ser decifrado com nenhuma chave de "
                "MFA_SECRET_KEYS (%s). O segundo fator desta conta fica indisponível e o "
                "login dela depende do código de recuperação.",
                "valor com forma de token Fernet — chave ausente da lista, ou rotação "
                "que removeu a chave antiga cedo demais"
                if parece_cifrado
                else "valor sem forma de token Fernet — provavelmente segredo em claro que a "
                "migration de cifra não alcançou; rode `alembic upgrade head`",
            )
            return None
