import pytest
from fastapi.testclient import TestClient

from homecareos.main import app

# Chave de teste compartilhada pelos módulos de teste que precisam autenticar
# contra a app real (`homecareos.main.app`), em vez da app isolada de
# `test_auth.py`. Não é segredo: só existe no processo de teste.
TEST_API_KEY = "chave-de-teste"
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
