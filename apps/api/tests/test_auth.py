"""Testes da autenticação por `X-API-Key`. Sem Postgres: a dependency é
exercitada contra uma app FastAPI mínima, isolada da app real — os endpoints
de negócio precisam de banco, a auth não.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from homecareos.api import auth as auth_module
from homecareos.api.auth import MENSAGEM_CREDENCIAL_INVALIDA, require_api_key
from homecareos.api.errors import register_exception_handlers
from homecareos.config import Settings, get_settings
from homecareos.main import create_app

CHAVE_VALIDA = "chave-valida-de-teste"


def _app_protegido(settings: Settings) -> FastAPI:
    """App mínima com uma única rota atrás de `require_api_key`."""
    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/protegido", dependencies=[Depends(require_api_key)])
    def protegido() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_app_protegido(Settings(api_keys=f"{CHAVE_VALIDA}, outra-chave")))


# --- AC1/AC2: presença, ausência e igualdade de corpo/status -----------------


def test_chave_valida_passa(client: TestClient) -> None:
    resposta = client.get("/protegido", headers={"X-API-Key": CHAVE_VALIDA})

    assert resposta.status_code == 200
    assert resposta.json() == {"ok": True}


def test_segunda_chave_da_lista_tambem_passa(client: TestClient) -> None:
    resposta = client.get("/protegido", headers={"X-API-Key": "outra-chave"})

    assert resposta.status_code == 200


def test_sem_header_responde_401(client: TestClient) -> None:
    resposta = client.get("/protegido")

    assert resposta.status_code == 401


def test_chave_errada_responde_401(client: TestClient) -> None:
    resposta = client.get("/protegido", headers={"X-API-Key": "chave-errada"})

    assert resposta.status_code == 401


def test_chave_ausente_e_chave_errada_devolvem_o_mesmo_corpo_e_status(
    client: TestClient,
) -> None:
    """Não pode dar para sondar se a chave existe: os dois casos são idênticos."""
    sem_header = client.get("/protegido")
    com_chave_errada = client.get("/protegido", headers={"X-API-Key": "quase-a-certa"})

    assert sem_header.status_code == com_chave_errada.status_code == 401
    assert sem_header.json() == com_chave_errada.json()


def test_corpo_do_401_nao_revela_qual_das_duas_falhas_ocorreu(client: TestClient) -> None:
    resposta = client.get("/protegido", headers={"X-API-Key": "errada"})

    corpo = resposta.json()
    texto = str(corpo).lower()
    assert "credencial inválida" in texto or "credencial invalida" in texto
    # nada de "ausente"/"não enviada" vs. "expirada"/"inválida" — mesma mensagem sempre.
    assert "ausente" not in texto
    assert "não enviada" not in texto


def test_chave_vazia_no_header_e_tratada_como_invalida(client: TestClient) -> None:
    resposta = client.get("/protegido", headers={"X-API-Key": ""})

    assert resposta.status_code == 401


# --- comparação em tempo constante -------------------------------------------


def test_auth_compara_com_hmac_compare_digest_nunca_com_igualdade_de_string() -> None:
    """Trava a implementação: comparação de string comum vaza o prefixo certo
    por tempo de resposta. Verificação estática do código-fonte de `auth.py`."""
    fonte = inspect.getsource(auth_module)

    assert "hmac.compare_digest" in fonte
    # Não pode haver comparação `recebida == chave` nem `chave == recebida`
    # nem uso de `in` sobre a lista de chaves como critério de aceite.
    assert " recebida == " not in fonte
    assert " == recebida" not in fonte
    assert "recebida in " not in fonte


# --- boot: api_keys vazio fora de `local` ------------------------------------


def test_api_keys_vazio_em_local_nao_impede_o_boot() -> None:
    create_app(Settings(environment="local", api_keys=""))


def test_api_keys_vazio_em_production_impede_o_boot() -> None:
    with pytest.raises(RuntimeError, match="api_keys"):
        create_app(Settings(environment="production", api_keys=""))


def test_api_keys_vazio_em_homolog_tambem_impede_o_boot() -> None:
    with pytest.raises(RuntimeError):
        create_app(Settings(environment="homolog", api_keys=""))


def test_api_keys_preenchido_em_production_sobe_normalmente() -> None:
    create_app(Settings(environment="production", api_keys="chave-de-producao"))


# --- OpenAPI: esquema de segurança declarado (AC7) ---------------------------


def test_openapi_declara_o_esquema_de_seguranca_x_api_key() -> None:
    app = create_app(Settings(environment="local", api_keys="chave-qualquer"))

    schema = app.openapi()

    esquemas = schema["components"]["securitySchemes"]
    assert esquemas["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    # `/health` é a única rota sem exigência de segurança.
    for path, operacoes in schema["paths"].items():
        for metodo, operacao in operacoes.items():
            if path == "/health":
                continue
            assert operacao.get("security"), f"{metodo.upper()} {path} não exige X-API-Key"


def test_health_nao_exige_seguranca_no_openapi() -> None:
    app = create_app(Settings(environment="local", api_keys="chave-qualquer"))

    schema = app.openapi()

    assert not schema["paths"]["/health"]["get"].get("security")


# --- /health continua fora da autenticação -----------------------------------


def test_health_responde_200_sem_x_api_key() -> None:
    app = create_app(Settings(environment="local", api_keys="chave-qualquer"))
    client = TestClient(app)

    resposta = client.get("/health")

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok"}


def test_id_aleatorio_nao_e_confundido_com_chave_valida(client: TestClient) -> None:
    resposta = client.get("/protegido", headers={"X-API-Key": str(uuid.uuid4())})

    assert resposta.status_code == 401


@pytest.mark.parametrize(
    "chave_hostil",
    ["café", "chave-com-acentuação", "🔑", "Ã©"],
)
def test_chave_nao_ascii_devolve_401_e_nao_erro_interno(chave_hostil: str) -> None:
    """O valor do header é controlado por quem chama, e chega como `str`.

    Na rede real o header trafega como bytes e o Starlette o decodifica em
    latin-1, então a aplicação pode receber uma `str` não-ASCII mesmo que um
    cliente HTTP em Python se recuse a enviá-la. Comparando `str`,
    `hmac.compare_digest` levantaria `TypeError` ali e a resposta viraria 500
    — distinguível de um 401, e portanto um sinal para quem sonda. Por isso
    este teste chama a dependência direto: o `TestClient` não consegue
    reproduzir o caminho, mas ele existe.
    """
    settings = Settings(api_keys=CHAVE_VALIDA)

    with pytest.raises(HTTPException) as erro:
        require_api_key(settings=settings, x_api_key=chave_hostil)

    assert erro.value.status_code == 401
    assert erro.value.detail == MENSAGEM_CREDENCIAL_INVALIDA
