from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from homecareos.config import get_settings
from homecareos.main import app

# Chave de teste compartilhada pelos módulos de teste que precisam autenticar
# contra a app real (`homecareos.main.app`), em vez da app isolada de
# `test_auth.py`. Não é segredo: só existe no processo de teste.
TEST_API_KEY = "chave-de-teste"
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}

# Chaves Fernet do processo de teste, que cifram `usuarios.mfa_secret` em
# repouso (ADR 0008). Literais, e não `Fernet.generate_key()` a cada execução,
# pelo mesmo motivo de `TEST_API_KEY`: um teste que falha precisa falhar igual
# na próxima vez. Não são segredo — estão num arquivo público e não protegem
# banco nenhum.
#
# A SEGUNDA existe para os testes de ROTAÇÃO: cifrar com ela, configurar as duas
# com a primeira na frente, e provar que o segredo antigo continua sendo lido.
TEST_MFA_SECRET_KEY = "TQ4iEbAaOOOLpBXpLTUpVo96A_ceAHVh6P6xUXA7lXA="
TEST_MFA_SECRET_KEY_ANTIGA = "n4mQMkG7bJDgs7AiN0BdU1Jt7ND9BQNhCJdiZOwlprc="


def configurar_chaves_mfa(monkeypatch: pytest.MonkeyPatch, chaves: str) -> None:
    """Põe `chaves` em `MFA_SECRET_KEYS` e invalida o `Settings` cacheado.

    O `cache_clear` é obrigatório e não é detalhe: `db/cifra.py` resolve a chave
    por `get_settings()` — o singleton do processo —, e **não** pela dependency
    injetada. Tem que ser assim: `SegredoCifrado` roda dentro do SQLAlchemy, sem
    request e sem `dependency_overrides` por perto, então um teste que mexesse só
    no override estaria medindo uma configuração que a cifra nunca leria.

    `chaves=""` é um valor legítimo aqui — é o cenário "sem chave configurada" —,
    e a variável de ambiente vazia tem precedência sobre o `.env`, então o teste
    vale mesmo numa máquina cujo `.env` tenha a chave preenchida.
    """
    monkeypatch.setenv("MFA_SECRET_KEYS", chaves)
    get_settings.cache_clear()


@pytest.fixture
def chave_mfa(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A cifra do segredo TOTP ligada, com uma chave só. Devolve a chave."""
    configurar_chaves_mfa(monkeypatch, TEST_MFA_SECRET_KEY)
    yield TEST_MFA_SECRET_KEY
    # O `monkeypatch` restaura a variável de ambiente depois deste teardown; o
    # `cache_clear` daqui garante que o próximo `get_settings()` releia o
    # ambiente já restaurado, em vez de servir o `Settings` deste teste.
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
