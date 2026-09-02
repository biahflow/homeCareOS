"""Serviço de intake: recebe um upload e devolve N documentos em `processando`.

```text
validar_upload (magic bytes, tamanho)
  → split_pages                      (N páginas)
  → storage.put(build_key(...))      (uma vez por página)
  → N Documento(status=processando, pagina=n, idempotency_key=f"{chave}:{n}")
  → COMMIT                           ← o upload está garantido a partir daqui
  → dispatcher.dispatch(...)         ← falha aqui NÃO desfaz o que está acima
```

Duas decisões deste módulo não são detalhe de implementação:

**O commit acontece antes da extração.** Uma exceção do provider de extração
não pode derrubar o upload: o documento já está gravado, e o técnico que
fotografou o prontuário já foi embora da casa do paciente. Perder o upload
porque a IA falhou trocaria um problema recuperável (reprocessar a extração
depois) por um irrecuperável (pedir o documento de novo). A falha vira uma
linha em `log_conferencia` e o documento fica em `processando`.

**A idempotência é decidida pelo banco.** `documentos.idempotency_key` é único
e um upload cria N documentos, então a chave do cliente é derivada por página
(`f"{chave}:{pagina}"`). A colisão é detectada pelo `IntegrityError` do índice
único, não por um SELECT prévio: dois uploads simultâneos com a mesma chave
passariam os dois pelo SELECT, e só o banco decide qual dos dois ganha.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from homecareos.db.models import Documento, DocumentoStatus, TipoDocumento
from homecareos.intake.dispatcher import ExtractionDispatcher
from homecareos.intake.errors import IdempotencyConflictError
from homecareos.intake.pdf import PageImage, split_pages
from homecareos.intake.repository import DocumentoRegistrado, DocumentoRepository
from homecareos.intake.validation import validar_upload
from homecareos.storage import DocumentStorage, build_key

USUARIO_SISTEMA = "sistema"
ACAO_EXTRACAO_FALHOU = "extracao_falhou"

_MAX_DETALHE = 500
"""Teto do texto de auditoria — a mensagem vem de uma exceção de terceiro."""

_EXTENSOES = {"image/png": ".png", "image/jpeg": ".jpg"}
"""O que `split_pages` devolve: PDF vira PNG por página, foto continua JPEG."""


@dataclass(frozen=True)
class ResultadoUpload:
    """Documentos do upload e se eles já existiam antes desta requisição."""

    documentos: list[DocumentoRegistrado]
    ja_existia: bool
    """`True` quando a chave de idempotência já havia criado estes documentos —
    a API responde 200 em vez de 201, e a extração não é disparada de novo."""


def _chave_da_pagina(idempotency_key: str | None, pagina: int) -> str | None:
    """Deriva a chave por página. Sem chave do cliente, grava `NULL`.

    O índice único do Postgres aceita múltiplos `NULL`: sem `Idempotency-Key`
    não há promessa de deduplicação, e o upload segue o fluxo normal.
    """
    if idempotency_key is None:
        return None
    return f"{idempotency_key}:{pagina}"


def receber_upload(
    *,
    conteudo: bytes,
    filename: str,
    competencia: str,
    idempotency_key: str | None,
    repository: DocumentoRepository,
    storage: DocumentStorage,
    dispatcher: ExtractionDispatcher,
    tipo: TipoDocumento = TipoDocumento.EVOLUCAO,
) -> ResultadoUpload:
    """Ingere um upload: valida, fatia em páginas, persiste e dispara a extração.

    `tipo` default `EVOLUCAO` porque é o documento que o técnico envia para
    comprovar a visita; a classificação automática do tipo é outro módulo
    (`homecareos.classification`), fora do escopo desta fase.
    """
    validar_upload(conteudo, filename)
    paginas = split_pages(conteudo)

    # Curto-circuito do reenvio: se as páginas já existem, sai antes de gravar
    # no storage. Sem isso, cada replay deixa N cópias órfãs do prontuário no
    # bucket — o banco recusa depois, mas os objetos já foram escritos, e
    # cópia não referenciada de documento clínico é justamente o que não se
    # quer acumulando. Isto é otimização, não a regra: a autoridade sobre a
    # colisão continua sendo o índice único (ver `_resolver_colisao`), porque
    # dois uploads simultâneos passariam os dois por esta consulta.
    ja_registrados = _buscar_existentes(repository, idempotency_key, paginas)
    if ja_registrados is not None:
        return ja_registrados

    documentos = [
        _montar_documento(
            pagina=pagina,
            competencia=competencia,
            idempotency_key=idempotency_key,
            tipo=tipo,
            storage=storage,
        )
        for pagina in paginas
    ]

    try:
        registrados = repository.criar_documentos(documentos)
    except IntegrityError as exc:
        repository.desfazer()
        return _resolver_colisao(repository, idempotency_key, paginas, exc)

    _disparar_extracao(repository, dispatcher, registrados, paginas)
    return ResultadoUpload(documentos=registrados, ja_existia=False)


def _montar_documento(
    *,
    pagina: PageImage,
    competencia: str,
    idempotency_key: str | None,
    tipo: TipoDocumento,
    storage: DocumentStorage,
) -> Documento:
    """Grava a página no storage e monta o `Documento` que aponta para ela.

    O `sha256` da chave é do conteúdo da **página renderizada**, não do arquivo
    original: é ele que identifica o objeto que está sendo gravado.
    """
    documento_id = uuid.uuid4()
    sha256 = hashlib.sha256(pagina.conteudo).hexdigest()
    extensao = _EXTENSOES.get(pagina.content_type, ".bin")
    chave = build_key(documento_id, sha256, extensao)
    storage.put(chave, pagina.conteudo, pagina.content_type)
    return Documento(
        id=documento_id,
        tipo=tipo,
        arquivo_url=chave,
        competencia=competencia,
        status=DocumentoStatus.PROCESSANDO,
        pagina=pagina.numero,
        idempotency_key=_chave_da_pagina(idempotency_key, pagina.numero),
    )


def _buscar_existentes(
    repository: DocumentoRepository,
    idempotency_key: str | None,
    paginas: list[PageImage],
) -> ResultadoUpload | None:
    """Devolve o resultado do reenvio quando todas as páginas já existem.

    `None` significa "siga o fluxo normal" — inclusive quando só parte das
    páginas existe, caso em que a colisão é levantada aqui mesmo, antes de
    qualquer escrita no storage.
    """
    if idempotency_key is None:
        return None

    chaves = [_chave_da_pagina(idempotency_key, p.numero) for p in paginas]
    existentes = repository.buscar_por_idempotency_keys([c for c in chaves if c is not None])
    if not existentes:
        return None
    if len(existentes) == len(paginas):
        return ResultadoUpload(documentos=existentes, ja_existia=True)
    raise IdempotencyConflictError(
        f"Idempotency-Key já usado por um upload de {len(existentes)} página(s); "
        f"este upload tem {len(paginas)}"
    )


def _resolver_colisao(
    repository: DocumentoRepository,
    idempotency_key: str | None,
    paginas: list[PageImage],
    erro: IntegrityError,
) -> ResultadoUpload:
    """Decide o que a violação de unicidade significou.

    Todas as páginas já existentes é o reenvio esperado: devolve o que já está
    gravado, sem criar documento novo e sem chamar a extração de novo (cada
    chamada custa dinheiro). Nenhuma existente significa que o `IntegrityError`
    não veio da idempotência — a exceção original sobe.
    """
    chaves = [_chave_da_pagina(idempotency_key, pagina.numero) for pagina in paginas]
    existentes = repository.buscar_por_idempotency_keys([c for c in chaves if c is not None])
    if len(existentes) == len(paginas) and existentes:
        return ResultadoUpload(documentos=existentes, ja_existia=True)
    if existentes:
        raise IdempotencyConflictError(
            f"Idempotency-Key já usado por um upload de {len(existentes)} página(s); "
            f"este upload tem {len(paginas)}"
        ) from erro
    raise erro


def _disparar_extracao(
    repository: DocumentoRepository,
    dispatcher: ExtractionDispatcher,
    registrados: list[DocumentoRegistrado],
    paginas: list[PageImage],
) -> None:
    """Dispara a extração de cada página. Falha aqui não desfaz o upload."""
    for documento, pagina in zip(registrados, paginas, strict=True):
        try:
            dispatcher.dispatch(documento.id, pagina)
        except Exception as exc:
            # Nenhuma falha de extração pode derrubar um upload já commitado.
            repository.registrar_log(
                documento_id=documento.id,
                acao=ACAO_EXTRACAO_FALHOU,
                usuario=USUARIO_SISTEMA,
                detalhe=f"{type(exc).__name__}: {exc}"[:_MAX_DETALHE],
            )
