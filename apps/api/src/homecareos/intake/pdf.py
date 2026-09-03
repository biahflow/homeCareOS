"""Split de um documento de intake em páginas — uma evolução por página.

O técnico comprova a visita pelo prontuário (evolução clínica com data,
carimbo COREN e assinatura), que chega como PDF escaneado, foto ou imagem. Um
PDF de 10 páginas é 10 evoluções distintas, não um documento só: cada página
vira sua própria imagem PNG, que a trilha de integração (fora de escopo aqui)
grava como um documento independente.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import pymupdf

from homecareos.config import get_settings
from homecareos.intake.errors import InvalidDocumentError, UnsupportedMediaTypeError
from homecareos.intake.validation import DetectedType, detect_type


@dataclass(frozen=True)
class PageImage:
    """Uma página já renderizada como PNG, pronta para ir ao storage."""

    numero: int
    conteudo: bytes
    content_type: str
    largura: int
    altura: int


def split_pages(data: bytes, *, dpi: int | None = None) -> list[PageImage]:
    """Fatia um documento de intake em páginas.

    PDF vira uma imagem PNG por página, renderizada em `dpi` (por padrão,
    `settings.pdf_render_dpi`). PNG e JPEG passam direto, sem reencodar: a
    foto do prontuário tirada pelo técnico chega em JPEG, e convertê-la para
    PNG multiplicaria o tamanho várias vezes sem ganho de qualidade — inflando
    storage e o corpo da requisição enviada ao modelo de visão.
    """
    detected = detect_type(data)
    if detected is None:
        raise UnsupportedMediaTypeError("Conteúdo não reconhecido como PDF, JPEG ou PNG")
    if detected is DetectedType.PDF:
        return _split_pdf(data, dpi=dpi)
    if detected is DetectedType.PNG:
        largura, altura = _png_dimensions(data)
        return [
            PageImage(
                numero=1,
                conteudo=data,
                content_type="image/png",
                largura=largura,
                altura=altura,
            )
        ]
    return [_jpeg_to_page(data)]


def _split_pdf(data: bytes, *, dpi: int | None) -> list[PageImage]:
    resolved_dpi = dpi if dpi is not None else get_settings().pdf_render_dpi
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")  # type: ignore[no-untyped-call]
    except Exception as exc:
        raise InvalidDocumentError("PDF corrompido ou ilegível") from exc
    try:
        if doc.needs_pass:
            raise InvalidDocumentError("PDF protegido por senha não pode ser processado")
        if doc.page_count < 1:
            raise InvalidDocumentError("PDF não possui páginas")

        matrix = pymupdf.Matrix(resolved_dpi / 72, resolved_dpi / 72)  # type: ignore[no-untyped-call]
        pages: list[PageImage] = []
        for index in range(doc.page_count):
            page = doc.load_page(index)  # type: ignore[no-untyped-call]
            pixmap = page.get_pixmap(matrix=matrix, alpha=False, colorspace=pymupdf.csRGB)
            pages.append(
                PageImage(
                    numero=index + 1,
                    conteudo=pixmap.tobytes("png"),
                    content_type="image/png",
                    largura=pixmap.width,
                    altura=pixmap.height,
                )
            )
        return pages
    finally:
        doc.close()  # type: ignore[no-untyped-call]


def _jpeg_to_page(data: bytes) -> PageImage:
    """Mantém o JPEG original; usa o PyMuPDF só para descobrir as dimensões."""
    try:
        pixmap = pymupdf.Pixmap(data)  # type: ignore[no-untyped-call]
    except Exception as exc:
        raise InvalidDocumentError("Imagem JPEG corrompida ou ilegível") from exc
    return PageImage(
        numero=1,
        conteudo=data,
        content_type="image/jpeg",
        largura=pixmap.width,
        altura=pixmap.height,
    )


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Lê largura/altura do chunk IHDR sem decodificar a imagem inteira."""
    if len(data) < 24:
        raise InvalidDocumentError("PNG inválido: cabeçalho incompleto")
    width, height = struct.unpack(">II", data[16:24])
    return width, height
