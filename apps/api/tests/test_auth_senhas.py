"""Testes do hash de senha (Argon2id). Sem Postgres: é função pura.

Nenhum teste deste módulo imprime senha, e o único literal de senha que existe
aqui é descartável e não abre nada — o que se afirma é justamente que ela não
sobrevive em lugar nenhum depois do hash.
"""

from __future__ import annotations

from homecareos.auth import senhas

SENHA = "uma-senha-de-teste-42"


def test_verificar_aceita_a_senha_correta() -> None:
    assert senhas.verificar(senhas.gerar_hash(SENHA), SENHA) is True


def test_verificar_recusa_a_senha_errada() -> None:
    assert senhas.verificar(senhas.gerar_hash(SENHA), "senha-errada") is False


def test_dois_hashes_da_mesma_senha_sao_diferentes() -> None:
    """Sal por hash: sem ele, duas pessoas com a mesma senha teriam o mesmo
    hash, e um dump vazado entregaria as duas de uma vez."""
    assert senhas.gerar_hash(SENHA) != senhas.gerar_hash(SENHA)


def test_o_hash_nao_contem_a_senha_em_claro() -> None:
    """A afirmação que sustenta o critério de aceite: senha nunca é gravada em claro."""
    hash_gerado = senhas.gerar_hash(SENHA)

    assert SENHA not in hash_gerado
    assert hash_gerado.startswith("$argon2id$")


def test_verificar_com_hash_malformado_devolve_false_em_vez_de_levantar() -> None:
    """Hash corrompido no banco não pode virar 500: um status distinguível diria
    a quem sonda quais contas têm hash quebrado."""
    assert senhas.verificar("nao-e-um-hash-argon2", SENHA) is False


def test_verificar_dummy_nao_levanta_e_nao_devolve_nada() -> None:
    """Ela existe para gastar tempo, não para decidir: o resultado é descartado."""
    assert senhas.verificar_dummy() is None
