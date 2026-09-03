from __future__ import annotations

import uuid
from pathlib import Path

import boto3
import pytest
from botocore.stub import ANY, Stubber

from homecareos.config import Settings
from homecareos.storage import (
    LocalDocumentStorage,
    S3DocumentStorage,
    StorageError,
    build_key,
    get_storage,
)


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "s3_endpoint_url": "http://localhost:9002",
        "s3_bucket": "homecareos-documentos",
        "s3_access_key": "minioadmin",
        "s3_secret_key": "minioadmin",
        "s3_region": "us-east-1",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_build_key_format() -> None:
    document_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    sha256 = "a" * 64

    key = build_key(document_id, sha256, ".png")

    assert key == f"documentos/{document_id}/{sha256}.png"


def test_local_storage_put_then_read_returns_same_bytes(tmp_path: Path) -> None:
    storage = LocalDocumentStorage(root=tmp_path)
    data = b"conteudo-da-pagina-renderizada"

    key = storage.put("documentos/doc-1/hash.png", data, "image/png")
    url = storage.presigned_url(key)

    assert url.startswith("file://")
    path = Path(url.removeprefix("file://"))
    assert path.read_bytes() == data


def test_local_storage_presigned_url_is_file_scheme(tmp_path: Path) -> None:
    storage = LocalDocumentStorage(root=tmp_path)
    key = storage.put("documentos/doc-1/hash.png", b"x", "image/png")

    url = storage.presigned_url(key, expires_in=60)

    assert url == f"file://{tmp_path / key}"


def _stubbed_s3(settings: Settings) -> tuple[S3DocumentStorage, Stubber]:
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
    )
    stubber = Stubber(client)
    storage = S3DocumentStorage(settings, client=client)
    return storage, stubber


def test_s3_storage_put_uses_bucket_and_key_from_settings() -> None:
    settings = _settings()
    storage, stubber = _stubbed_s3(settings)
    data = b"conteudo-png"
    key = "documentos/doc-1/hash.png"

    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": settings.s3_bucket,
            "Key": key,
            "Body": ANY,
            "ContentType": "image/png",
        },
    )
    with stubber:
        result = storage.put(key, data, "image/png")

    assert result == key
    stubber.assert_no_pending_responses()


def test_s3_storage_put_wraps_boto_error_in_storage_error() -> None:
    settings = _settings()
    storage, stubber = _stubbed_s3(settings)
    key = "documentos/doc-1/hash.png"

    stubber.add_client_error("put_object", service_error_code="AccessDenied")
    with stubber, pytest.raises(StorageError):
        storage.put(key, b"data", "image/png")


def test_s3_storage_presigned_url_does_not_touch_network() -> None:
    settings = _settings()
    storage, _stubber = _stubbed_s3(settings)
    key = "documentos/doc-1/hash.png"

    url = storage.presigned_url(key, expires_in=120)

    assert settings.s3_bucket in url
    assert key in url


def test_get_storage_returns_s3_when_credentials_configured() -> None:
    settings = _settings()

    storage = get_storage(settings)

    assert isinstance(storage, S3DocumentStorage)


def test_get_storage_falls_back_to_local_without_credentials() -> None:
    settings = _settings(s3_access_key="", s3_secret_key="")

    storage = get_storage(settings)

    assert isinstance(storage, LocalDocumentStorage)


def test_local_storage_recusa_chave_que_escapa_da_raiz(tmp_path: Path) -> None:
    """Parte da chave deriva do arquivo enviado pelo técnico; conteúdo de fora
    não pode decidir caminho de escrita."""
    storage = LocalDocumentStorage(root=tmp_path / "raiz")

    with pytest.raises(StorageError):
        storage.put("../fora.png", b"x", "image/png")


def test_get_storage_fora_de_local_usa_s3_mesmo_sem_credencial() -> None:
    """Em AWS a credencial vem de IAM role, não de chave estática. Cair para
    disco local aqui gravaria prontuário num diretório temporário."""
    settings = Settings(environment="production", s3_access_key="", s3_secret_key="")

    assert isinstance(get_storage(settings), S3DocumentStorage)
