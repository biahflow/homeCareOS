"""Testes do `POST /api/documentos`. Sem Postgres, sem MinIO, sem rede.

As dependências do router (repositório, storage e dispatcher) são substituídas
por `app.dependency_overrides`, então `get_session` nunca é chamado e nenhuma
conexão de banco é aberta.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from homecareos.config import Settings, get_settings
from homecareos.intake.router import (
    get_document_storage,
    get_documento_repository,
    get_extraction_dispatcher,
)
from homecareos.intake.service import ACAO_EXTRACAO_FALHOU
from homecareos.main import app
from tests.fakes import (
    FailingDispatcher,
    FailingStorage,
    FakeDispatcher,
    FakeDocumentoRepository,
    FakeStorage,
    make_pdf,
)

COMPETENCIA = "2024-03"


@pytest.fixture
def repository() -> FakeDocumentoRepository:
    return FakeDocumentoRepository()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


@pytest.fixture
def api(
    repository: FakeDocumentoRepository,
    storage: FakeStorage,
    dispatcher: FakeDispatcher,
) -> Iterator[TestClient]:
    app.dependency_overrides[get_documento_repository] = lambda: repository
    app.dependency_overrides[get_document_storage] = lambda: storage
    app.dependency_overrides[get_extraction_dispatcher] = lambda: dispatcher
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _upload(
    api: TestClient,
    conteudo: bytes,
    *,
    competencia: str | None = COMPETENCIA,
    idempotency_key: str | None = None,
    filename: str = "evolucao.pdf",
    content_type: str = "application/pdf",
):  # type: ignore[no-untyped-def]
    data = {} if competencia is None else {"competencia": competencia}
    headers = {} if idempotency_key is None else {"Idempotency-Key": idempotency_key}
    return api.post(
        "/api/documentos",
        files={"arquivo": (filename, conteudo, content_type)},
        data=data,
        headers=headers,
    )


# --- AC1: contrato da resposta -----------------------------------------------


def test_pdf_de_dez_paginas_responde_201_com_dez_documentos(
    api: TestClient, dispatcher: FakeDispatcher
) -> None:
    resposta = _upload(api, make_pdf(10))

    assert resposta.status_code == 201
    corpo = resposta.json()
    assert list(corpo) == ["documentos"]
    assert len(corpo["documentos"]) == 10
    assert [d["pagina"] for d in corpo["documentos"]] == list(range(1, 11))
    assert all(d["status"] == "processando" for d in corpo["documentos"])
    assert all(d["competencia"] == COMPETENCIA for d in corpo["documentos"])
    assert all(set(d) == {"id", "pagina", "status", "competencia"} for d in corpo["documentos"])
    assert len(dispatcher.chamadas) == 10


# --- AC4/AC5: idempotência ----------------------------------------------------


def test_reenvio_com_a_mesma_chave_responde_200_sem_criar_nem_reextrair(
    api: TestClient, repository: FakeDocumentoRepository, dispatcher: FakeDispatcher
) -> None:
    conteudo = make_pdf(3)

    primeira = _upload(api, conteudo, idempotency_key="chave-do-cliente")
    segunda = _upload(api, conteudo, idempotency_key="chave-do-cliente")

    assert primeira.status_code == 201
    assert segunda.status_code == 200
    assert [d["id"] for d in segunda.json()["documentos"]] == [
        d["id"] for d in primeira.json()["documentos"]
    ]
    assert len(repository.documentos) == 3
    assert len(dispatcher.chamadas) == 3


def test_reenvio_sem_header_cria_documentos_novos(
    api: TestClient, repository: FakeDocumentoRepository
) -> None:
    conteudo = make_pdf(2)

    primeira = _upload(api, conteudo)
    segunda = _upload(api, conteudo)

    assert (primeira.status_code, segunda.status_code) == (201, 201)
    ids_primeira = {d["id"] for d in primeira.json()["documentos"]}
    ids_segunda = {d["id"] for d in segunda.json()["documentos"]}
    assert ids_primeira.isdisjoint(ids_segunda)
    assert len(repository.documentos) == 4


def test_mesma_chave_para_arquivo_com_outro_numero_de_paginas_responde_409(
    api: TestClient,
) -> None:
    _upload(api, make_pdf(3), idempotency_key="chave-do-cliente")

    resposta = _upload(api, make_pdf(5), idempotency_key="chave-do-cliente")

    assert resposta.status_code == 409


# --- AC6: falha de extração não derruba o upload ------------------------------


def test_falha_de_extracao_ainda_responde_201(
    repository: FakeDocumentoRepository, storage: FakeStorage
) -> None:
    app.dependency_overrides[get_documento_repository] = lambda: repository
    app.dependency_overrides[get_document_storage] = lambda: storage
    app.dependency_overrides[get_extraction_dispatcher] = lambda: FailingDispatcher()
    try:
        resposta = _upload(TestClient(app), make_pdf(2))
    finally:
        app.dependency_overrides.clear()

    assert resposta.status_code == 201
    assert all(d["status"] == "processando" for d in resposta.json()["documentos"])
    assert len(repository.logs) == 2
    assert {log["acao"] for log in repository.logs} == {ACAO_EXTRACAO_FALHOU}


# --- AC7/AC8: mapeamento de erro ---------------------------------------------


def test_competencia_ausente_responde_422(api: TestClient) -> None:
    resposta = _upload(api, make_pdf(1), competencia=None)

    assert resposta.status_code == 422


@pytest.mark.parametrize("competencia", ["2024-3", "03/2024", "2024-13", "2024-00", "", "marco"])
def test_competencia_fora_do_formato_responde_422(api: TestClient, competencia: str) -> None:
    resposta = _upload(api, make_pdf(1), competencia=competencia)

    assert resposta.status_code == 422


def test_arquivo_txt_responde_415(api: TestClient) -> None:
    resposta = _upload(
        api,
        b"conteudo texto puro",
        filename="evolucao.txt",
        content_type="text/plain",
    )

    assert resposta.status_code == 415


def test_pdf_corrompido_responde_422(api: TestClient) -> None:
    resposta = _upload(api, b"%PDF-1.4 lixo")

    assert resposta.status_code == 422


def test_arquivo_acima_do_limite_responde_413_sem_ler_o_corpo_inteiro(
    api: TestClient,
) -> None:
    """O corte barato usa o tamanho declarado pelo parser multipart."""
    app.dependency_overrides[get_settings] = lambda: Settings(max_upload_bytes=16)

    resposta = _upload(api, make_pdf(1))

    assert resposta.status_code == 413


def test_arquivo_acima_do_limite_detectado_na_validacao_responde_413(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mesmo passando pelo corte barato, `validar_upload` derruba com 413."""
    monkeypatch.setattr(
        "homecareos.intake.validation.get_settings",
        lambda: Settings(max_upload_bytes=16),
    )

    resposta = _upload(api, make_pdf(1))

    assert resposta.status_code == 413


def test_storage_indisponivel_responde_503(
    repository: FakeDocumentoRepository, dispatcher: FakeDispatcher
) -> None:
    app.dependency_overrides[get_documento_repository] = lambda: repository
    app.dependency_overrides[get_document_storage] = lambda: FailingStorage()
    app.dependency_overrides[get_extraction_dispatcher] = lambda: dispatcher
    try:
        resposta = _upload(TestClient(app), make_pdf(1))
    finally:
        app.dependency_overrides.clear()

    assert resposta.status_code == 503
    assert repository.documentos == {}


def test_health_continua_respondendo_com_o_router_registrado(api: TestClient) -> None:
    assert api.get("/health").json() == {"status": "ok"}
