from __future__ import annotations

import pymupdf
import pytest

from homecareos.intake.errors import (
    InvalidDocumentError,
    UnsupportedMediaTypeError,
    UploadTooLargeError,
)
from homecareos.intake.pdf import split_pages
from homecareos.intake.validation import DetectedType, validar_upload

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _make_pdf(num_pages: int) -> bytes:
    doc = pymupdf.open()
    try:
        for index in range(num_pages):
            page = doc.new_page()
            page.insert_text((72, 72), f"evolucao {index + 1}")
        return doc.tobytes()
    finally:
        doc.close()


def _make_encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    try:
        doc.new_page()
        return doc.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="segredo",
            owner_pw="segredo",
        )
    finally:
        doc.close()


def _make_png() -> bytes:
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "evolucao")
        return page.get_pixmap().tobytes("png")
    finally:
        doc.close()


def _make_jpeg() -> bytes:
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "evolucao")
        return page.get_pixmap().tobytes("jpg")
    finally:
        doc.close()


def test_split_pages_pdf_with_ten_pages_returns_ten_page_images() -> None:
    data = _make_pdf(10)

    pages = split_pages(data)

    assert len(pages) == 10
    assert [p.numero for p in pages] == list(range(1, 11))
    for page in pages:
        assert page.content_type == "image/png"
        assert page.conteudo.startswith(_PNG_MAGIC)
        assert page.largura > 0
        assert page.altura > 0


def test_split_pages_png_returns_single_page_without_reencoding() -> None:
    data = _make_png()

    pages = split_pages(data)

    assert len(pages) == 1
    assert pages[0].numero == 1
    assert pages[0].content_type == "image/png"
    assert pages[0].conteudo == data  # não reencodado: bytes idênticos ao original


def test_split_pages_jpeg_returns_single_page_without_reencoding() -> None:
    """JPEG passa direto: reencodar para PNG multiplicaria o tamanho da foto
    tirada pelo técnico sem ganho de qualidade."""
    data = _make_jpeg()
    assert data.startswith(_JPEG_MAGIC)

    pages = split_pages(data)

    assert len(pages) == 1
    assert pages[0].numero == 1
    assert pages[0].content_type == "image/jpeg"
    assert pages[0].conteudo == data
    assert pages[0].largura > 0 and pages[0].altura > 0


def test_split_pages_encrypted_pdf_raises_invalid_document_error() -> None:
    data = _make_encrypted_pdf()

    with pytest.raises(InvalidDocumentError):
        split_pages(data)


def test_split_pages_corrupted_pdf_raises_invalid_document_error() -> None:
    data = b"%PDF-1.4 isto nao e um pdf valido"

    with pytest.raises(InvalidDocumentError):
        split_pages(data)


def test_split_pages_unsupported_content_raises_unsupported_media_type_error() -> None:
    data = b"conteudo qualquer que nao e pdf nem imagem"

    with pytest.raises(UnsupportedMediaTypeError):
        split_pages(data)


def test_split_pages_uses_custom_dpi() -> None:
    data = _make_pdf(1)

    pages_default = split_pages(data)
    pages_high_dpi = split_pages(data, dpi=400)

    assert pages_high_dpi[0].largura > pages_default[0].largura
    assert pages_high_dpi[0].altura > pages_default[0].altura


def test_validar_upload_detects_pdf() -> None:
    data = _make_pdf(1)

    detected = validar_upload(data, "evolucao.pdf")

    assert detected is DetectedType.PDF


def test_validar_upload_ignores_extension_and_trusts_magic_bytes() -> None:
    png_data = _make_png()

    detected = validar_upload(png_data, "evolucao.pdf")

    assert detected is DetectedType.PNG


def test_validar_upload_rejects_unsupported_type() -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        validar_upload(b"conteudo texto puro", "evolucao.txt")


def test_validar_upload_rejects_file_above_size_limit() -> None:
    data = _make_pdf(1)
    oversized = data + b"0" * (32 * 1024 * 1024)

    with pytest.raises(UploadTooLargeError):
        validar_upload(oversized, "evolucao.pdf")
