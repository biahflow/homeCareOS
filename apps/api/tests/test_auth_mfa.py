"""Testes unitários do segundo fator (issue #35) — sem banco e sem relógio real.

`agora` é sempre um instante fixo passado por parâmetro: o anti-replay e a
janela de tolerância são aritmética sobre o passo TOTP, e provar isso com
`datetime.now()` deixaria o teste falhando de madrugada, na virada do passo.

Nenhum teste daqui imprime segredo nem código de recuperação.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import unquote

import pyotp
import pytest

from homecareos.auth import mfa

# Segredo fixo: a suíte precisa derivar os mesmos códigos que a implementação,
# e um segredo aleatório por execução não acrescentaria nada — o que se prova
# aqui é a aritmética do passo, não a aleatoriedade do gerador.
SEGREDO = "JBSWY3DPEHPK3PXP"

# Instante fixo, escolhido no meio de um passo (segundo 15 de 30) de propósito:
# no limite exato do passo, um erro de arredondamento de 1 segundo passaria
# despercebido.
AGORA = datetime(2026, 9, 3, 12, 0, 15, tzinfo=UTC)

EMAIL = "ana@exemplo.com"
EMISSOR = "HomeCareOS"


def _passo_de(momento: datetime) -> int:
    return int(momento.timestamp()) // mfa.PASSO_SEGUNDOS


def _codigo_do_passo(passo: int) -> str:
    return pyotp.TOTP(SEGREDO).at(passo * mfa.PASSO_SEGUNDOS)


def _verificar(codigo: str, *, janela: int = 1, ultimo_passo: int | None = None) -> int | None:
    return mfa.verificar_codigo(
        SEGREDO, codigo, agora=AGORA, janela=janela, ultimo_passo=ultimo_passo
    )


# --- janela de aceitação ------------------------------------------------------


def test_codigo_do_momento_e_aceito_e_devolve_o_passo() -> None:
    passo = _passo_de(AGORA)

    assert _verificar(_codigo_do_passo(passo)) == passo


def test_passo_anterior_e_seguinte_sao_aceitos_com_janela_1() -> None:
    """Tolerância de relógio: o celular da pessoa adianta ou atrasa alguns segundos."""
    passo = _passo_de(AGORA)

    assert _verificar(_codigo_do_passo(passo - 1)) == passo - 1
    assert _verificar(_codigo_do_passo(passo + 1)) == passo + 1


def test_passo_fora_da_janela_e_recusado() -> None:
    """Cada passo a mais de tolerância são 30 segundos a mais de vida para um
    código já visto — é por isso que a janela é estreita e configurável."""
    passo = _passo_de(AGORA)

    assert _verificar(_codigo_do_passo(passo - 2)) is None
    assert _verificar(_codigo_do_passo(passo + 2)) is None


def test_janela_zero_aceita_so_o_passo_atual() -> None:
    passo = _passo_de(AGORA)

    assert _verificar(_codigo_do_passo(passo), janela=0) == passo
    assert _verificar(_codigo_do_passo(passo - 1), janela=0) is None


# --- anti-replay --------------------------------------------------------------


def test_codigo_do_passo_ja_usado_e_recusado() -> None:
    """O coração do anti-replay: sem isto, o mesmo código vale durante toda a
    janela e quem o interceptar tem ~90 segundos para reusá-lo."""
    passo = _passo_de(AGORA)

    assert _verificar(_codigo_do_passo(passo), ultimo_passo=passo) is None


def test_codigo_de_passo_anterior_ao_ultimo_aceito_tambem_e_recusado() -> None:
    """Aceitar passo anterior ao último reabriria a janela para trás — a mesma
    falha pelo outro lado."""
    passo = _passo_de(AGORA)

    assert _verificar(_codigo_do_passo(passo - 1), ultimo_passo=passo) is None


def test_passo_maior_que_o_ultimo_e_aceito() -> None:
    """O anti-replay não pode trancar a conta: o passo seguinte continua valendo."""
    passo = _passo_de(AGORA)

    assert _verificar(_codigo_do_passo(passo + 1), ultimo_passo=passo) == passo + 1


def test_sem_ultimo_passo_o_codigo_do_momento_e_aceito() -> None:
    """Primeira verificação da conta: `mfa_ultimo_passo` ainda é `NULL`."""
    passo = _passo_de(AGORA)

    assert _verificar(_codigo_do_passo(passo), ultimo_passo=None) == passo


# --- entrada malformada -------------------------------------------------------


@pytest.mark.parametrize(
    "codigo",
    [
        "",
        "   ",
        "abcdef",
        "12345",
        "1234567",
        "12 34 56",
        "12345a",
        # Dígito arábico-índico: `str.isdigit()` diz que é dígito, e
        # `hmac.compare_digest` levantaria `TypeError` com `str` fora de ASCII.
        "١٢٣٤٥٦",
        "🔐🔐🔐🔐🔐🔐",
    ],
)
def test_codigo_malformado_devolve_none_sem_levantar(codigo: str) -> None:
    """A entrada vem de quem chama a API: um 500 aqui seria resposta
    distinguível de um 401 — sinal para quem sonda."""
    assert _verificar(codigo) is None


def test_espaco_nas_pontas_nao_invalida_o_codigo() -> None:
    """Quem copia da tela do celular traz espaço junto; isso não é código errado."""
    passo = _passo_de(AGORA)

    assert _verificar(f"  {_codigo_do_passo(passo)} ") == passo


# --- URI do QR code -----------------------------------------------------------


def test_uri_otpauth_carrega_o_esquema_o_emissor_e_o_email() -> None:
    uri = mfa.uri_otpauth(SEGREDO, email=EMAIL, emissor=EMISSOR)

    # `unquote` porque o `@` do e-mail viaja percent-encoded na URI — é o
    # formato correto, e afirmar sobre o texto cru testaria o encoding.
    legivel = unquote(uri)
    assert legivel.startswith("otpauth://totp/")
    assert EMISSOR in legivel
    assert EMAIL in legivel
    assert SEGREDO in legivel


# --- códigos de recuperação ---------------------------------------------------


def test_gerar_codigos_devolve_a_quantidade_pedida_todos_distintos() -> None:
    codigos = mfa.gerar_codigos_recuperacao(8)

    assert len(codigos) == 8
    assert len(set(codigos)) == 8


def test_codigos_de_recuperacao_saem_no_formato_legivel_de_dois_blocos() -> None:
    """Alguém vai copiar isto de uma tela para um papel: bloco de cinco é o que
    se confere sem perder a conta."""
    for codigo in mfa.gerar_codigos_recuperacao(4):
        bloco_a, _, bloco_b = codigo.partition("-")
        assert len(bloco_a) == len(bloco_b) == 5
        assert codigo == mfa.normalizar_codigo_recuperacao(codigo)


def test_duas_geracoes_seguidas_nao_repetem_codigo() -> None:
    """`secrets`, e não `random`: a segunda lista não pode ser previsível a
    partir da primeira."""
    primeira = mfa.gerar_codigos_recuperacao(8)
    segunda = mfa.gerar_codigos_recuperacao(8)

    assert not set(primeira) & set(segunda)


def test_normalizar_codigo_aceita_maiuscula_e_espaco() -> None:
    assert mfa.normalizar_codigo_recuperacao("  A1B2C-3D4E5 ") == "a1b2c-3d4e5"
