"""`POST /api/documentos`: upload de evolução escaneada.

Mapeamento de erro para HTTP:

| Erro                        | HTTP |
| --------------------------- | ---- |
| `UploadTooLargeError`       | 413  |
| `UnsupportedMediaTypeError` | 415  |
| `InvalidDocumentError`      | 422  |
| `competencia` inválida      | 422  |
| `IdempotencyConflictError`  | 409  |
| `StorageError`              | 503  |
| falha de extração           | —    |

A última linha é a que importa: falha de extração **não** vira erro HTTP. O
documento já foi commitado antes de a extração ser disparada, e a requisição
responde 201 com ele em `processando`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
from homecareos.db.session import get_session
from homecareos.extraction.dispatcher import build_sync_dispatcher
from homecareos.intake.dispatcher import ExtractionDispatcher
from homecareos.intake.errors import (
    IdempotencyConflictError,
    InvalidDocumentError,
    UnsupportedMediaTypeError,
    UploadTooLargeError,
)
from homecareos.intake.repository import DocumentoRepository, SqlAlchemyDocumentoRepository
from homecareos.intake.schemas import DocumentoCriado, UploadResponse, competencia_valida
from homecareos.intake.service import receber_upload
from homecareos.storage import DocumentStorage, StorageError, get_storage

router = APIRouter(prefix="/api", tags=["documentos"])


def get_documento_repository(
    session: Annotated[Session, Depends(get_session)],
) -> DocumentoRepository:
    return SqlAlchemyDocumentoRepository(session=session)


def get_document_storage(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DocumentStorage:
    return get_storage(settings)


def get_extraction_dispatcher(
    storage: Annotated[DocumentStorage, Depends(get_document_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExtractionDispatcher:
    return build_sync_dispatcher(storage, settings)


@router.post(
    "/documentos",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingere uma evolução escaneada, uma página por documento",
)
def criar_documentos(
    response: Response,
    repository: Annotated[DocumentoRepository, Depends(get_documento_repository)],
    storage: Annotated[DocumentStorage, Depends(get_document_storage)],
    dispatcher: Annotated[ExtractionDispatcher, Depends(get_extraction_dispatcher)],
    settings: Annotated[Settings, Depends(get_settings)],
    arquivo: Annotated[UploadFile, File(description="PDF, JPEG ou PNG da evolução")],
    competencia: Annotated[str, Form(description="Competência do faturamento, `YYYY-MM`")],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> UploadResponse:
    """Cria um documento por página do arquivo enviado.

    A competência é campo de formulário obrigatório porque não é extraível do
    documento: a evolução traz a data do atendimento, não a competência em que
    ela será faturada.
    """
    if not competencia_valida(competencia):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"competencia {competencia!r} não está no formato YYYY-MM",
        )

    # Barra o arquivo grande antes de lê-lo inteiro na memória. `validar_upload`
    # confere o tamanho de novo sobre os bytes reais — este é só o corte barato.
    if arquivo.size is not None and arquivo.size > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Arquivo acima do limite de {settings.max_upload_bytes} bytes",
        )

    conteudo = arquivo.file.read()

    try:
        resultado = receber_upload(
            conteudo=conteudo,
            filename=arquivo.filename or "upload",
            competencia=competencia,
            idempotency_key=idempotency_key,
            repository=repository,
            storage=storage,
            dispatcher=dispatcher,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except UnsupportedMediaTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except InvalidDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage de documentos indisponível",
        ) from exc

    if resultado.ja_existia:
        # Reenvio com a mesma chave: nada foi criado agora, então não é 201.
        response.status_code = status.HTTP_200_OK

    return UploadResponse(
        documentos=[DocumentoCriado.de_registrado(doc) for doc in resultado.documentos]
    )
