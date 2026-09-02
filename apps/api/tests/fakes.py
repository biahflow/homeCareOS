"""Dublês do intake: storage, repositório e dispatcher — nenhum precisa de container.

O repositório é a peça que precisa de mais cuidado: ele reproduz o
comportamento que decide o desenho da idempotência, o `IntegrityError` do
índice único de `documentos.idempotency_key`. O serviço nunca consulta o banco
antes de inserir, então é essa exceção (e só ela) que o teste precisa
oferecer para exercitar o caminho de reenvio.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pymupdf
from sqlalchemy.exc import IntegrityError

from homecareos.db.models import Documento
from homecareos.intake.pdf import PageImage
from homecareos.intake.repository import DocumentoRegistrado
from homecareos.storage import StorageError


def make_pdf(num_pages: int) -> bytes:
    """PDF sintético com `num_pages` páginas, cada uma com um texto distinto."""
    doc = pymupdf.open()
    try:
        for index in range(num_pages):
            page = doc.new_page()
            page.insert_text((72, 72), f"evolucao {index + 1}")
        return doc.tobytes()
    finally:
        doc.close()


def make_png() -> bytes:
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "evolucao")
        return page.get_pixmap().tobytes("png")
    finally:
        doc.close()


@dataclass
class FakeStorage:
    """Guarda os objetos num dicionário e registra a ordem das gravações."""

    objetos: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    chaves: list[str] = field(default_factory=list)

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.objetos[key] = (data, content_type)
        self.chaves.append(key)
        return key

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return f"memory://{key}"


@dataclass
class FailingStorage:
    """Storage indisponível — o caminho que precisa virar 503."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        raise StorageError(f"storage indisponível ao gravar {key!r}")

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        raise StorageError("storage indisponível")


@dataclass
class FakeDocumentoRepository:
    """Repositório em memória com a mesma regra de unicidade do Postgres."""

    documentos: dict[uuid.UUID, DocumentoRegistrado] = field(default_factory=dict)
    por_chave: dict[str, DocumentoRegistrado] = field(default_factory=dict)
    logs: list[dict[str, str]] = field(default_factory=list)
    rollbacks: int = 0

    def criar_documentos(self, documentos: list[Documento]) -> list[DocumentoRegistrado]:
        chaves = [d.idempotency_key for d in documentos if d.idempotency_key is not None]
        if any(chave in self.por_chave for chave in chaves):
            # Mesma exceção que o índice único levanta: a colisão é decidida
            # pelo banco, nunca por um SELECT prévio do serviço.
            raise IntegrityError(
                "INSERT INTO documentos ...",
                {},
                Exception(
                    "duplicate key value violates unique constraint "
                    '"documentos_idempotency_key_key"'
                ),
            )
        registrados = [
            DocumentoRegistrado(
                id=documento.id,
                pagina=documento.pagina if documento.pagina is not None else 1,
                status=documento.status,
                competencia=documento.competencia,
            )
            for documento in documentos
        ]
        for documento, registrado in zip(documentos, registrados, strict=True):
            self.documentos[registrado.id] = registrado
            if documento.idempotency_key is not None:
                self.por_chave[documento.idempotency_key] = registrado
        return registrados

    def desfazer(self) -> None:
        self.rollbacks += 1

    def buscar_por_idempotency_keys(self, chaves: list[str]) -> list[DocumentoRegistrado]:
        encontrados = [self.por_chave[chave] for chave in chaves if chave in self.por_chave]
        return sorted(encontrados, key=lambda documento: documento.pagina)

    def registrar_log(
        self, *, documento_id: uuid.UUID, acao: str, usuario: str, detalhe: str
    ) -> None:
        self.logs.append(
            {
                "documento_id": str(documento_id),
                "acao": acao,
                "usuario": usuario,
                "detalhe": detalhe,
            }
        )


@dataclass
class FakeDispatcher:
    """Conta as chamadas — é a contagem que prova que o reenvio não re-extrai."""

    chamadas: list[tuple[uuid.UUID, int]] = field(default_factory=list)

    def dispatch(self, documento_id: uuid.UUID, pagina: PageImage) -> None:
        self.chamadas.append((documento_id, pagina.numero))


@dataclass
class FailingDispatcher:
    """Extração que estoura. Não pode derrubar um upload já commitado."""

    erro: Exception = field(default_factory=lambda: RuntimeError("provider de extração caiu"))
    chamadas: list[uuid.UUID] = field(default_factory=list)

    def dispatch(self, documento_id: uuid.UUID, pagina: PageImage) -> None:
        self.chamadas.append(documento_id)
        raise self.erro
