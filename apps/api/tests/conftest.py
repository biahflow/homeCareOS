import pytest
from fastapi.testclient import TestClient

from homecareos.main import app

# Chave de teste compartilhada pelos módulos de teste que precisam autenticar
# contra a app real (`homecareos.main.app`), em vez da app isolada de
# `test_auth.py`. Não é segredo: só existe no processo de teste.
TEST_API_KEY = "chave-de-teste"
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}

# Papéis que a chave de teste carrega (`API_KEY_PAPEIS`, ADR 0007). Os três,
# porque é o que reproduz o acesso total que a chave tinha antes do escopo por
# papel — o que estes módulos já assumiam quando foram escritos. Quem testa o
# estreitamento em si declara o próprio valor (ver `test_autorizacao_papeis.py`).
TEST_API_KEY_PAPEIS = "conferente,coordenador,gestor"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
