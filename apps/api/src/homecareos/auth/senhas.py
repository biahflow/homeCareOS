"""Hash e verificação de senha com Argon2id — a parte crítica desta trilha.

Regras não negociáveis, cada uma com uma razão concreta:

- **Argon2id, nunca `hmac.compare_digest`.** `api/auth.py` compara segredo
  compartilhado de alta entropia, onde o problema é o tempo de comparação.
  Aqui o segredo é escolhido por uma pessoa, tem entropia baixa e é atacável
  por dicionário sobre um dump de banco: o que protege é função lenta, com sal
  por senha e custo de memória — exatamente o que o Argon2id faz.
- **A senha nunca é logada, nem parcialmente, nem em mensagem de erro** — e por
  isso `verificar` não propaga a exceção da biblioteca: o `repr` dela em um
  traceback carregaria o hash, e um `except` desatento carregaria a senha.
- Os parâmetros de custo são os **defaults da biblioteca**, deliberadamente. Um
  valor escolhido à mão aqui envelheceria sozinho; o default do `argon2-cffi`
  acompanha a recomendação corrente a cada atualização de dependência.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError, VerificationError

_hasher = PasswordHasher()

# Hash fixo de uma senha descartável, gerado na importação do módulo, usado só
# por `verificar_dummy()`. Não é segredo e não abre nada: não existe usuário com
# ela.
_HASH_DUMMY = _hasher.hash("senha-que-nao-abre-nada")


def gerar_hash(senha: str) -> str:
    """Devolve o hash Argon2id da senha (com sal aleatório novo a cada chamada)."""
    return _hasher.hash(senha)


def verificar(hash_armazenado: str, senha: str) -> bool:
    """`True` se a senha corresponde ao hash. Nunca levanta, nunca loga a senha.

    Hash malformado (linha corrompida, migração de dado malfeita) devolve
    `False` como qualquer outra falha: um 500 ali seria uma resposta
    distinguível — quem sondasse o login descobriria pelo status quais contas
    têm hash quebrado.
    """
    try:
        return _hasher.verify(hash_armazenado, senha)
    except (VerificationError, InvalidHashError, Argon2Error):
        return False


def verificar_dummy() -> None:
    """Gasta o tempo de uma verificação Argon2 e descarta o resultado.

    Existe para o login com e-mail **inexistente** custar o mesmo que o login
    com senha errada. Sem isso, o tempo de resposta diz se um e-mail está
    cadastrado — Argon2 leva ordens de grandeza mais que um `SELECT` que não
    achou nada — e a lista de quem trabalha na operação vaza por cronômetro,
    sem nenhuma credencial válida.
    """
    verificar(_HASH_DUMMY, "senha-qualquer")
