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
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import IO, Any, Protocol

from homecareos.config import Settings, get_settings

CHUNK_SIZE = 64 * 1024
"""Tamanho do bloco de leitura.

Uma evolução escaneada não é pequena e a API serve o arquivo para quem
confere: ler em blocos mantém o pico de memória constante por download, em vez
de proporcional ao tamanho do documento vezes o número de conferentes olhando
prontuário ao mesmo tempo.
"""

CONTENT_TYPE_PADRAO = "application/octet-stream"

_CONTENT_TYPE_POR_EXTENSAO = {".png": "image/png", ".jpg": "image/jpeg"}
"""Inverso do mapa que escolhe a extensão na gravação
(`intake.service._EXTENSOES`): PDF vira PNG por página, foto continua JPEG."""

_CODIGOS_DE_OBJETO_AUSENTE = frozenset({"NoSuchKey", "NotFound", "404"})
"""Códigos de erro do S3 que significam "esse objeto não existe".

`NoSuchBucket` fica de fora de propósito: bucket que não existe é
infraestrutura quebrada, não documento que sumiu — e é bom que apareça como
falha do storage, não como um 404 por documento.
"""


class StorageError(RuntimeError):
    """O storage respondeu com erro (indisponível, sem permissão, sem objeto)."""


class ObjectNotFoundError(StorageError):
    """A chave não está no storage.

    Subclasse de `StorageError` para não escapar de quem já trata a família
    inteira, mas com identidade própria porque significa outra coisa: o storage
    respondeu, e a resposta foi "não tenho esse objeto". Um documento cujo
    arquivo sumiu do bucket é 404 para quem o pediu, não 503 do storage.
    """


class DocumentStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str: ...

    def get(self, key: str) -> Iterator[bytes]:
        """Devolve o conteúdo do objeto em blocos, sem carregá-lo na memória.

        Contrato que as duas implementações respeitam e de que quem serve o
        arquivo depende: **a procura acontece nesta chamada**, não na primeira
        iteração. `ObjectNotFoundError` sai daqui, antes de o primeiro byte ir
        para a resposta.

        Um `get` que fosse ele próprio um gerador adiaria a procura para dentro
        do corpo já em transmissão — com o status HTTP escolhido e enviado —, e
        "a chave não está no storage" não teria mais como virar 404. É a mesma
        classe de armadilha que `reports.router._stream_csv` documenta do outro
        lado (a sessão do banco já fechada quando o corpo começa a sair).
        """
        ...

    def presigned_url(self, key: str, expires_in: int = 3600) -> str: ...


def content_type_for_key(key: str) -> str:
    """Content-Type do objeto deduzido da extensão da chave.

    A extensão é escolhida na gravação a partir do content type real da página
    (`intake.service._EXTENSOES`), então ela é a informação que sobreviveu ao
    armazenamento: nem o `LocalDocumentStorage` guarda o content type, nem o
    banco tem coluna para ele. Extensão desconhecida (inclusive o `.bin` do
    caso inesperado) cai em `application/octet-stream` — servir prontuário como
    um tipo adivinhado é pior do que deixar o navegador perguntar o que fazer.
    """
    return _CONTENT_TYPE_POR_EXTENSAO.get(PurePosixPath(key).suffix.lower(), CONTENT_TYPE_PADRAO)


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

    def get(self, key: str) -> Iterator[bytes]:
        """Abre o objeto agora e devolve o corpo em blocos (ver o Protocol).

        `get_object` já falha aqui quando a chave não existe, e é isso que
        permite distinguir "documento sem arquivo" (404) de "storage fora do
        ar" (503) antes de a resposta começar a sair.
        """
        client = self._get_client()
        try:
            resposta = client.get_object(Bucket=self._settings.s3_bucket, Key=key)
        except Exception as exc:
            if _e_objeto_ausente(exc):
                raise ObjectNotFoundError(f"Objeto {key!r} não está no storage") from exc
            raise StorageError(f"Falha ao ler {key!r} do storage") from exc
        return _blocos_do_body(resposta["Body"])

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

    def get(self, key: str) -> Iterator[bytes]:
        """Abre o arquivo agora e devolve o conteúdo em blocos (ver o Protocol).

        A existência é checada e o descritor é aberto **antes** de devolver o
        iterador, pelo mesmo motivo do backend S3: chave ausente precisa virar
        exceção enquanto ainda dá para responder 404.
        """
        path = self._resolve(key)
        if not path.is_file():
            raise ObjectNotFoundError(f"Objeto {key!r} não está no storage")
        try:
            arquivo = path.open("rb")
        except OSError as exc:
            # Existe, mas não abriu (permissão, disco): é falha do storage, e
            # não "o documento não está aqui".
            raise StorageError(f"Falha ao ler {key!r} do storage") from exc
        return _blocos_do_arquivo(arquivo)

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return f"file://{self._resolve(key)}"


def _e_objeto_ausente(exc: Exception) -> bool:
    """Reconhece o "esse objeto não existe" do S3 sem importar `botocore` aqui.

    O `ClientError` do botocore carrega o código em
    `response["Error"]["Code"]`; olhar o atributo em vez do tipo mantém este
    módulo com o mesmo import preguiçoso de `boto3` que `_get_client` já usa, e
    não amarra a checagem a uma versão do pacote.
    """
    resposta = getattr(exc, "response", None)
    if not isinstance(resposta, dict):
        return False
    erro = resposta.get("Error")
    if not isinstance(erro, dict):
        return False
    return str(erro.get("Code", "")) in _CODIGOS_DE_OBJETO_AUSENTE


def _blocos_do_body(body: Any) -> Iterator[bytes]:
    """Transmite o corpo devolvido pelo S3 em blocos e fecha a conexão no fim."""
    try:
        yield from body.iter_chunks(CHUNK_SIZE)
    finally:
        body.close()


def _blocos_do_arquivo(arquivo: IO[bytes]) -> Iterator[bytes]:
    """Transmite o arquivo já aberto em blocos e fecha o descritor no fim."""
    with arquivo:
        while bloco := arquivo.read(CHUNK_SIZE):
            yield bloco


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
