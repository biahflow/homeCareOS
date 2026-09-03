"""Implementação síncrona da porta de disparo declarada pelo intake.

`SyncExtractionDispatcher` extrai na própria requisição e grava `extracoes` em
transação própria — nunca na sessão do intake, que já commitou os documentos
antes de chegar aqui. É o ponto exato que uma futura fila substitui: o intake
continua chamando `dispatch(...)` e não fica sabendo.

Este módulo satisfaz `homecareos.intake.dispatcher.ExtractionDispatcher`
estruturalmente, sem importá-lo: a extração não depende do intake, e o intake
não depende da extração.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from homecareos.classification.service import classificar_documento
from homecareos.config import Settings, get_settings
from homecareos.db.models import Documento
from homecareos.db.session import get_sessionmaker
from homecareos.extraction.provider import ExtractionProvider, get_provider
from homecareos.extraction.repository import registrar_extracao
from homecareos.extraction.s3_raw_store import S3RawResponseStore
from homecareos.extraction.schema import EvolucaoProntuario, PaginaDocumento
from homecareos.rules.engine import validar
from homecareos.rules.repository import buscar_regras_ativas, registrar_validacoes
from homecareos.storage import DocumentStorage


@dataclass
class SyncExtractionDispatcher:
    """Chama o provider na hora e grava o resultado em `extracoes`."""

    provider: ExtractionProvider
    session_factory: Callable[[], Session]

    def dispatch(self, documento_id: uuid.UUID, pagina: PaginaDocumento) -> None:
        resultado = self.provider.extract(pagina, str(documento_id))
        with self.session_factory() as session:
            registrar_extracao(session, documento_id, resultado)
            self._validar_contra_regras(session, documento_id, resultado.campos)

    def _validar_contra_regras(
        self, session: Session, documento_id: uuid.UUID, campos: EvolucaoProntuario
    ) -> None:
        """Encadeia o motor de regras (issue #5) e a classificação (issue #7).

        `registrar_extracao` já commitou sua própria transação (propriedade
        exclusiva de `extracoes`, ver `extraction/repository.py`) — não é
        possível estender essa transação sem tocar num arquivo fora do escopo
        desta trilha. Reusar a mesma sessão dentro do mesmo bloco de
        `dispatch` é a aproximação praticável do requisito "mesma transação
        da extração"; vale igual para `registrar_validacoes`, que também
        commita sozinho antes de a classificação rodar.

        LIMITAÇÃO CONHECIDA: as duas saídas antecipadas abaixo (documento sem
        `operadora_id` e operadora sem regra ativa) deixam o documento parado
        em `processando`, sem classificação e sem pendência — não há como
        classificar sem saber contra o que validar. `operadora_id` nulo é
        comum na ingestão atual; o documento fica visível na listagem por
        status e só sai de `processando` quando alguém associar a operadora e
        chamar `POST /api/documentos/{id}/revalidar`.
        """
        documento = session.get(Documento, documento_id)
        if documento is None or documento.operadora_id is None:
            return
        regras = buscar_regras_ativas(session, documento.operadora_id)
        if not regras:
            return
        resultados = validar(campos, regras, competencia=documento.competencia)
        registrar_validacoes(session, documento_id, resultados)
        classificar_documento(session, documento_id, resultados, usuario="sistema")


def build_sync_dispatcher(
    storage: DocumentStorage,
    settings: Settings | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> SyncExtractionDispatcher:
    """Monta o dispatcher síncrono já com o raw store em S3 ligado."""
    resolved_settings = settings if settings is not None else get_settings()
    factory: sessionmaker[Session] | Callable[[], Session] = (
        session_factory if session_factory is not None else get_sessionmaker()
    )
    provider = get_provider(resolved_settings, S3RawResponseStore(storage))
    return SyncExtractionDispatcher(provider=provider, session_factory=factory)
