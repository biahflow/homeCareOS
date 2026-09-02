"""Validação de upload: detecção de tipo por magic bytes e limite de tamanho.

O arquivo chega de fora (upload do técnico, na hora de comprovar a visita) e
não é confiável: a extensão do nome no formulário não prova nada sobre o
conteúdo enviado. Por isso o tipo é sempre decidido pelos primeiros bytes do
arquivo, nunca pelo nome — um `.pdf` cujo conteúdo é outra coisa tem que ser
rejeitado ou reclassificado corretamente, não aceito de olhos fechados.
"""

from __future__ import annotations

from enum import StrEnum

from homecareos.config import get_settings
from homecareos.intake.errors import UnsupportedMediaTypeError, UploadTooLargeError

_PDF_MAGIC = b"%PDF-"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


class DetectedType(StrEnum):
    """Tipo detectado pelos magic bytes do conteúdo."""

    PDF = "application/pdf"
    JPEG = "image/jpeg"
    PNG = "image/png"


def detect_type(data: bytes) -> DetectedType | None:
    """Detecta o tipo pelos magic bytes; `None` quando não é PDF, JPEG nem PNG."""
    if data.startswith(_PDF_MAGIC):
        return DetectedType.PDF
    if data.startswith(_PNG_MAGIC):
        return DetectedType.PNG
    if data.startswith(_JPEG_MAGIC):
        return DetectedType.JPEG
    return None


def validar_upload(data: bytes, filename: str) -> DetectedType:
    """Valida um upload: tamanho dentro do limite e tipo suportado por conteúdo.

    `filename` entra apenas nas mensagens de erro — nunca decide o tipo.
    """
    settings = get_settings()
    if len(data) > settings.max_upload_bytes:
        raise UploadTooLargeError(
            f"Arquivo {filename!r} tem {len(data)} bytes, acima do limite de "
            f"{settings.max_upload_bytes} bytes"
        )
    detected = detect_type(data)
    if detected is None:
        raise UnsupportedMediaTypeError(
            f"Arquivo {filename!r} não é PDF, JPEG ou PNG (tipo não reconhecido pelo conteúdo)"
        )
    return detected
