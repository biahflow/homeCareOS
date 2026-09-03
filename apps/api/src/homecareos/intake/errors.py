"""Erros de negócio do módulo de intake (validação de upload e split de páginas).

Todo erro previsível do intake herda de :class:`IntakeError`, para que a camada
de API (fora de escopo desta trilha) possa capturar uma família só de exceções
e traduzi-la em uma resposta HTTP, em vez de deixar uma exceção genérica do
PyMuPDF ou do parser de imagem estourar como 500.
"""

from __future__ import annotations


class IntakeError(Exception):
    """Erro de negócio no processamento de um documento de intake."""


class UploadTooLargeError(IntakeError):
    """O arquivo excede `settings.max_upload_bytes`."""


class UnsupportedMediaTypeError(IntakeError):
    """O conteúdo não é PDF, JPEG ou PNG (decidido pelos magic bytes, não pelo nome)."""


class InvalidDocumentError(IntakeError):
    """O documento tem o tipo certo mas está corrompido, vazio ou protegido por senha."""


class IdempotencyConflictError(IntakeError):
    """O `Idempotency-Key` já foi usado por um upload com outro número de páginas.

    Reenvio idêntico colide em **todas** as páginas e é respondido com os
    documentos já existentes. Colidir em apenas parte delas significa que a
    mesma chave está sendo reaproveitada para um arquivo diferente — a chave
    prometia "a mesma requisição", e devolver o resultado do upload anterior
    como se fosse deste esconderia a troca.
    """
