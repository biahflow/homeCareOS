"""Exception handlers que traduzem todo erro da API para o envelope padronizado.

Cobre três origens, sem alterar nenhum arquivo que levanta as exceções:

- `HTTPException` (inclui as levantadas por `require_api_key` e pelo router
  de intake existente, `homecareos.intake.router`);
- erro de validação de request do FastAPI/Pydantic (`RequestValidationError`);
- as famílias de erro de domínio já existentes, `IntakeError` e `StorageError`
  — hoje o único router que as levanta (`intake.router`) já as captura e
  converte em `HTTPException` antes de saírem dele, então estes dois handlers
  são uma rede de segurança para qualquer endpoint futuro que deixe uma delas
  escapar, não um caminho exercitado pelo `POST /api/documentos` atual.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from homecareos.api.responses import erro_envelope
from homecareos.intake.errors import (
    IdempotencyConflictError,
    IntakeError,
    InvalidDocumentError,
    UnsupportedMediaTypeError,
    UploadTooLargeError,
)
from homecareos.storage import StorageError

_TIPOS_POR_STATUS: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "payload_too_large",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "unprocessable_entity",
    status.HTTP_429_TOO_MANY_REQUESTS: "too_many_requests",
    status.HTTP_501_NOT_IMPLEMENTED: "not_implemented",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}


def _tipo_do_status(status_code: int) -> str:
    if status_code in _TIPOS_POR_STATUS:
        return _TIPOS_POR_STATUS[status_code]
    return "internal_error" if status_code >= 500 else "http_error"


def _status_do_intake_error(exc: IntakeError) -> int:
    if isinstance(exc, UploadTooLargeError):
        return status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    if isinstance(exc, UnsupportedMediaTypeError):
        return status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    if isinstance(exc, IdempotencyConflictError):
        return status.HTTP_409_CONFLICT
    if isinstance(exc, InvalidDocumentError):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return status.HTTP_422_UNPROCESSABLE_ENTITY


def register_exception_handlers(app: FastAPI) -> None:
    """Registra os handlers no `app`. Chamado uma vez, na criação da aplicação."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        mensagem = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=erro_envelope(tipo=_tipo_do_status(exc.status_code), mensagem=mensagem),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=erro_envelope(
                tipo="unprocessable_entity",
                mensagem="parâmetros inválidos",
                detalhes=jsonable_encoder(exc.errors()),
            ),
        )

    @app.exception_handler(IntakeError)
    async def _intake_error_handler(request: Request, exc: IntakeError) -> JSONResponse:
        status_code = _status_do_intake_error(exc)
        return JSONResponse(
            status_code=status_code,
            content=erro_envelope(tipo=_tipo_do_status(status_code), mensagem=str(exc)),
        )

    @app.exception_handler(StorageError)
    async def _storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=erro_envelope(
                tipo="service_unavailable",
                mensagem="Storage de documentos indisponível",
            ),
        )
