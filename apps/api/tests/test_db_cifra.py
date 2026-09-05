"""A cifra do segredo TOTP em repouso (ADR 0008), sem banco.

Exercita `SegredoCifrado` chamando `process_bind_param` / `process_result_value`
direto. Não precisa de Postgres: o que se prova aqui é a tradução nas duas
pontas, e ela é pura — o dialeto entra só porque a assinatura do SQLAlchemy o
exige, e nenhum caminho o consulta.

Nenhum teste daqui imprime segredo ou chave, e as asserções verificam a
**ausência** do segredo no valor gravado em vez de compará-lo com um esperado.
"""

from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.dialects import postgresql

from homecareos.config import get_settings
from homecareos.db import cifra
from tests.conftest import (
    TEST_MFA_SECRET_KEY,
    TEST_MFA_SECRET_KEY_ANTIGA,
    configurar_chaves_mfa,
)

# O segredo TOTP de exemplo. Base32, como `mfa.gerar_segredo()` devolve — é o
# formato de que `uri_otpauth` e todo app autenticador dependem, e a cifra não
# pode mudá-lo do lado do código.
SEGREDO = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

DIALETO = postgresql.dialect()


@pytest.fixture
def tipo() -> cifra.SegredoCifrado:
    return cifra.SegredoCifrado()


def _cifrar(tipo: cifra.SegredoCifrado, valor: str) -> str:
    guardado = tipo.process_bind_param(valor, DIALETO)
    assert guardado is not None
    return guardado


# --- None continua None -------------------------------------------------------


def test_none_continua_none_nos_dois_sentidos(tipo: cifra.SegredoCifrado, chave_mfa: str) -> None:
    """`NULL` é o que sinaliza "MFA não iniciado" em `auth/router.py`.

    Traduzi-lo para qualquer outra coisa mudaria o significado da coluna: quem
    nunca ativou o segundo fator passaria a ter segredo, e `/mfa/confirmar`
    deixaria de responder 422 para quem não iniciou nada.
    """
    assert tipo.process_bind_param(None, DIALETO) is None
    assert tipo.process_result_value(None, DIALETO) is None


def test_none_na_escrita_nao_exige_chave(
    tipo: cifra.SegredoCifrado, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Desativar o MFA (`mfa_secret = None`) precisa funcionar sem chave.

    É o contrário de gravar em claro: não há segredo para proteger, e recusar
    aqui trancaria quem quer justamente *desligar* o segundo fator.
    """
    configurar_chaves_mfa(monkeypatch, "")

    assert tipo.process_bind_param(None, DIALETO) is None


# --- ida e volta --------------------------------------------------------------


def test_o_valor_gravado_e_token_fernet_e_nao_contem_o_segredo(
    tipo: cifra.SegredoCifrado, chave_mfa: str
) -> None:
    """Critério de aceite 1, na unidade: o que vai para a coluna é opaco."""
    guardado = _cifrar(tipo, SEGREDO)

    assert guardado.startswith(cifra.PREFIXO_FERNET)
    assert SEGREDO not in guardado


def test_o_que_foi_cifrado_volta_igual(tipo: cifra.SegredoCifrado, chave_mfa: str) -> None:
    guardado = _cifrar(tipo, SEGREDO)

    assert tipo.process_result_value(guardado, DIALETO) == SEGREDO


def test_a_cifra_nao_e_deterministica_e_as_duas_formas_decifram(
    tipo: cifra.SegredoCifrado, chave_mfa: str
) -> None:
    """Dois `encrypt` do mesmo segredo produzem tokens diferentes — e é o certo.

    Fernet põe IV e timestamp novos a cada chamada. Um token determinístico
    entregaria, num dump, quais contas compartilham segredo — e daria a quem
    tem o banco um oráculo de igualdade que a cifra existe para negar.

    O projeto não perde nada com isso porque nenhuma consulta compara
    `mfa_secret` por igualdade: só `IS NULL` / `IS NOT NULL`.
    """
    primeiro = _cifrar(tipo, SEGREDO)
    segundo = _cifrar(tipo, SEGREDO)

    assert primeiro != segundo
    assert tipo.process_result_value(primeiro, DIALETO) == SEGREDO
    assert tipo.process_result_value(segundo, DIALETO) == SEGREDO


# --- sem chave ----------------------------------------------------------------


def test_sem_chave_a_escrita_levanta_em_vez_de_gravar_em_claro(
    tipo: cifra.SegredoCifrado, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O comportamento central do ADR 0008.

    Degradar em silêncio para texto claro é pior que recusar: quem ativou o MFA
    achando que estava protegido não teria como descobrir que não estava.
    """
    configurar_chaves_mfa(monkeypatch, "")

    with pytest.raises(RuntimeError) as excinfo:
        tipo.process_bind_param(SEGREDO, DIALETO)

    mensagem = str(excinfo.value)
    assert "MFA_SECRET_KEYS" in mensagem
    # A mensagem ensina o comando que resolve, e não só o nome da variável.
    assert "Fernet.generate_key()" in mensagem
    assert SEGREDO not in mensagem


def test_sem_chave_a_leitura_degrada_para_none_em_vez_de_levantar(
    tipo: cifra.SegredoCifrado,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    chave_mfa: str,
) -> None:
    """É o que preserva a saída de emergência quando a chave se perde.

    `POST /api/auth/mfa/verificar` só tenta o TOTP quando há segredo e cai no
    código de recuperação quando não há. Levantar aqui derrubaria com 500 até
    esse caminho de volta — o único que sobra para quem perdeu a chave.
    """
    guardado = _cifrar(tipo, SEGREDO)
    configurar_chaves_mfa(monkeypatch, "")

    with caplog.at_level(logging.ERROR, logger="homecareos.db.cifra"):
        assert tipo.process_result_value(guardado, DIALETO) is None

    assert "MFA_SECRET_KEYS" in caplog.text
    # O valor da coluna nunca vai para o log: ilegível para nós, ele continua
    # sendo o segundo fator de alguém.
    assert guardado not in caplog.text


def test_a_leitura_com_chave_errada_degrada_para_none_e_avisa_no_log(
    tipo: cifra.SegredoCifrado,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    chave_mfa: str,
) -> None:
    """Rotação que removeu a chave antiga cedo demais — o erro mais provável."""
    guardado = _cifrar(tipo, SEGREDO)
    configurar_chaves_mfa(monkeypatch, TEST_MFA_SECRET_KEY_ANTIGA)

    with caplog.at_level(logging.ERROR, logger="homecareos.db.cifra"):
        assert tipo.process_result_value(guardado, DIALETO) is None

    assert "token Fernet" in caplog.text
    assert guardado not in caplog.text


def test_segredo_em_claro_na_coluna_e_diagnosticado_como_migration_faltando(
    tipo: cifra.SegredoCifrado,
    caplog: pytest.LogCaptureFixture,
    chave_mfa: str,
) -> None:
    """Um valor sem forma de token Fernet aponta para outra causa, e o log diz qual.

    "Chave errada" e "a migration não rodou nesta linha" pedem ações opostas, e
    quem lê o log às três da manhã não deve ter que adivinhar de qual se trata.
    """
    with caplog.at_level(logging.ERROR, logger="homecareos.db.cifra"):
        assert tipo.process_result_value(SEGREDO, DIALETO) is None

    assert "alembic upgrade head" in caplog.text


# --- rotação ------------------------------------------------------------------


def test_a_chave_antiga_continua_decifrando_depois_de_a_nova_entrar(
    tipo: cifra.SegredoCifrado, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critério de aceite 4, na unidade: a razão de `MultiFernet` existir aqui.

    Com `Fernet` simples, trocar a chave exigiria downtime e um script de
    emergência para reescrever a coluna antes do deploy. Com a lista, rotacionar
    é pôr a nova na frente e deixar a antiga até nenhum segredo depender dela.
    """
    configurar_chaves_mfa(monkeypatch, TEST_MFA_SECRET_KEY_ANTIGA)
    guardado_com_a_antiga = _cifrar(tipo, SEGREDO)

    configurar_chaves_mfa(monkeypatch, f"{TEST_MFA_SECRET_KEY},{TEST_MFA_SECRET_KEY_ANTIGA}")

    assert tipo.process_result_value(guardado_com_a_antiga, DIALETO) == SEGREDO


def test_a_primeira_da_lista_e_a_que_cifra(
    tipo: cifra.SegredoCifrado, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sem isto, rotacionar não terminaria nunca: o dado novo continuaria
    nascendo com a chave que se quer aposentar."""
    configurar_chaves_mfa(monkeypatch, f"{TEST_MFA_SECRET_KEY},{TEST_MFA_SECRET_KEY_ANTIGA}")
    guardado = _cifrar(tipo, SEGREDO)

    # Só a chave da frente abre o que acabou de ser gravado.
    assert Fernet(TEST_MFA_SECRET_KEY).decrypt(guardado.encode()).decode() == SEGREDO
    with pytest.raises(InvalidToken):
        Fernet(TEST_MFA_SECRET_KEY_ANTIGA).decrypt(guardado.encode())


def test_espaco_entre_as_chaves_da_csv_nao_quebra_a_lista(
    tipo: cifra.SegredoCifrado, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env` escrito por pessoa tem espaço depois da vírgula.

    Sem o `strip`, a segunda chave viraria "chave inválida" e a aplicação
    recusaria subir no meio de uma rotação — que é justamente a hora em que
    ninguém quer descobrir um detalhe de parsing.
    """
    configurar_chaves_mfa(monkeypatch, f" {TEST_MFA_SECRET_KEY} , {TEST_MFA_SECRET_KEY_ANTIGA} ")

    assert tipo.process_result_value(_cifrar(tipo, SEGREDO), DIALETO) == SEGREDO


# --- configuração malformada --------------------------------------------------


def test_chave_malformada_levanta_dizendo_a_posicao_e_sem_ecoar_a_chave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Um typo não pode virar "sem chave": isso desligaria a cifra em silêncio,
    justamente para quem escreveu a variável porque queria cifrar.

    A posição na lista é o que torna a mensagem acionável numa rotação com duas
    chaves; a chave em si nunca aparece, porque saída de erro é o que se cola
    inteira num ticket de suporte.
    """
    quebrada = "isto-nao-e-uma-chave-fernet"
    configurar_chaves_mfa(monkeypatch, f"{TEST_MFA_SECRET_KEY},{quebrada}")

    with pytest.raises(RuntimeError) as excinfo:
        cifra.cifrador()

    mensagem = str(excinfo.value)
    assert "posição 2" in mensagem
    assert quebrada not in mensagem
    assert TEST_MFA_SECRET_KEY not in mensagem


def test_cifra_disponivel_responde_a_configuracao_em_vigor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """É esta função que `POST /api/auth/mfa/iniciar` consulta antes de gerar
    segredo, e ela precisa enxergar a mesma configuração que a coluna usa."""
    configurar_chaves_mfa(monkeypatch, "")
    assert cifra.cifra_disponivel() is False

    configurar_chaves_mfa(monkeypatch, TEST_MFA_SECRET_KEY)
    assert cifra.cifra_disponivel() is True


def test_csv_so_de_virgulas_e_espaco_conta_como_sem_chave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MFA_SECRET_KEYS=" , "` é `.env` mal editado, não configuração válida.

    O desfecho certo é "sem chave" — 503 em `/mfa/iniciar` e warning no boot —,
    e não um `MultiFernet` vazio, que a `cryptography` recusaria construir com
    um erro sem relação nenhuma com o que a pessoa fez.
    """
    configurar_chaves_mfa(monkeypatch, " , ")

    assert cifra.cifrador() is None


def test_a_configuracao_e_lida_do_settings_em_vigor_e_nao_de_um_cache_antigo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guarda contra a regressão mais provável deste módulo: memorizar o
    cifrador em variável de módulo em vez de derivá-lo da configuração.

    Se isso acontecesse, um processo que releia a configuração (ou este próprio
    teste) continuaria cifrando com a chave aposentada — e a rotação pareceria
    funcionar até o dia em que a chave antiga saísse da lista.
    """
    configurar_chaves_mfa(monkeypatch, TEST_MFA_SECRET_KEY)
    primeiro = cifra.cifrador()

    configurar_chaves_mfa(monkeypatch, TEST_MFA_SECRET_KEY_ANTIGA)
    segundo = cifra.cifrador()

    assert primeiro is not segundo
    assert get_settings().mfa_secret_keys == TEST_MFA_SECRET_KEY_ANTIGA
