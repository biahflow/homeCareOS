"""Storage de documentos: MinIO local, S3 em produção.

Fica fora do pacote `intake` de propósito: a trilha de intake grava a página
renderizada, e a trilha de extração (outra trilha) também precisa gravar no
S3 — a resposta bruta do modelo de IA. Storage é infraestrutura compartilhada
pelas duas, não um detalhe de intake.

A chave do objeto é `documentos/{uuid}/{sha256}{ext}`: o `sha256` é do
conteúdo da página (dedup e integridade), e o `uuid` mantém páginas do mesmo
documento agrupadas sob um prefixo sem depender do nome original do arquivo.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any, Protocol

from homecareos.config import Settings, get_settings


class StorageError(RuntimeError):
    """O storage respondeu com erro (indisponível, sem permissão, sem objeto)."""


class DocumentStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str: ...

    def presigned_url(self, key: str, expires_in: int = 3600) -> str: ...


def build_key(document_id: uuid.UUID, sha256: str, ext: str) -> str:
    """Monta a chave do objeto: `documentos/{uuid}/{sha256}{ext}`.

    `ext` já deve incluir o ponto (ex.: `.png`). `sha256` é o hash do
    conteúdo gravado (a página renderizada), não do arquivo original enviado.
    """
    return f"documentos/{document_id}/{sha256}{ext}"


class S3DocumentStorage:
    """Storage em S3/MinIO via boto3.

    Endpoint, bucket e credenciais vêm de `Settings`: endpoint vazio significa
    S3 real (a URL padrão do boto3 é usada nesse caso); com endpoint definido,
    aponta para o MinIO local. Qualquer erro do boto3 vira `StorageError` —
    falha fechada, nunca engole a exceção original.
    """

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        """`client` permite injetar um boto3 client (ou stub) já pronto — usado nos
        testes para nunca tocar a rede real."""
        self._settings = settings
        self._client: Any = client

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self._settings.s3_endpoint_url or None,
                # Credencial vazia vira `None` de propósito: assim o boto3 cai na
                # cadeia padrão (IAM role da instância, IRSA, perfil do ambiente).
                # Passar string vazia forçaria autenticação anônima e quebraria em
                # qualquer deploy AWS que use role em vez de chave estática.
                aws_access_key_id=self._settings.s3_access_key or None,
                aws_secret_access_key=self._settings.s3_secret_key or None,
                region_name=self._settings.s3_region,
            )
        return self._client

    def put(self, key: str, data: bytes, content_type: str) -> str:
        client = self._get_client()
        try:
            client.put_object(
                Bucket=self._settings.s3_bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except Exception as exc:
            raise StorageError(f"Falha ao gravar {key!r} no storage") from exc
        return key

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        client = self._get_client()
        try:
            url: str = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._settings.s3_bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as exc:
            raise StorageError(f"Falha ao gerar URL assinada para {key!r}") from exc
        return url


class LocalDocumentStorage:
    """Storage em disco temporário — existe para os testes rodarem sem rede.

    `presigned_url` devolve uma URL `file://` apontando para o arquivo local,
    o suficiente para o consumidor abrir o conteúdo sem distinguir o backend.
    """

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            root = Path(tempfile.mkdtemp(prefix="homecareos-storage-"))
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """Resolve a chave dentro da raiz, recusando qualquer coisa que escape dela.

        A chave nasce de `build_key()`, mas parte dela deriva do arquivo enviado
        pelo técnico. Conteúdo vindo de fora não decide caminho de escrita.
        """
        root = self._root.resolve()
        path = (root / key).resolve()
        if path != root and root not in path.parents:
            raise StorageError(f"Chave {key!r} escapa da raiz do storage")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return f"file://{self._resolve(key)}"


def get_storage(settings: Settings | None = None) -> DocumentStorage:
    """Escolhe a implementação de storage pela configuração.

    Fora de `local`, é **sempre** S3 — mesmo sem chave configurada, porque em
    AWS a credencial normalmente vem de IAM role e não de chave estática. Cair
    para disco local nesse caso gravaria prontuário num diretório temporário
    que some no próximo restart, silenciosamente: perder documento de
    comprovação é pior do que falhar alto.

    Disco local só quando o ambiente é `local` e não há credencial nenhuma.
    """
    resolved_settings = settings if settings is not None else get_settings()
    sem_credencial = not (resolved_settings.s3_access_key and resolved_settings.s3_secret_key)
    if resolved_settings.environment == "local" and sem_credencial:
        return LocalDocumentStorage()
    return S3DocumentStorage(resolved_settings)
