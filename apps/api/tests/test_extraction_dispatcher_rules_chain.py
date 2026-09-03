"""Prova o encadeamento extração → motor de regras dentro de `dispatch()`.

Arquivo novo, fora da lista original de arquivos do handoff: não existia
nenhum teste de regressão para `SyncExtractionDispatcher.dispatch()` no repo
(`test_extraction.py` testa só o provider). Mudar o comportamento de
`dispatch()` sem cobertura deixaria sem teste a peça mais importante do
encadeamento — sem isso o motor de regras é código morto.

Usa uma `Connection` compartilhada entre múltiplas `Session`s: `dispatch()`
abre sua própria sessão via `session_factory()`, que precisa ser diferente da
sessão de asserção do teste mas apontar para a mesma transação/conexão, para
tudo ficar dentro do rollback final. Cada `Session` usa
`join_transaction_mode="create_savepoint"` pelo mesmo motivo do
`test_rules_router.py`: `registrar_extracao` e `rules.repository` fazem
`session.commit()` internamente.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Connection, select
from sqlalchemy.orm import Session

from homecareos.db.models import Documento, Operadora, Regra, TipoDocumento, Validacao
from homecareos.db.models.enums import DocumentoStatus, ResultadoValidacao
from homecareos.db.session import get_engine
from homecareos.extraction.dispatcher import SyncExtractionDispatcher
from homecareos.extraction.schema import EvolucaoProntuario, ExtractionResult, PaginaDocumento


@dataclass
class SimplePage:
    """A implementação mínima de `PaginaDocumento` usada pelos testes (ver `test_extraction.py`)."""

    numero: int
    conteudo: bytes
    content_type: str


@dataclass
class _FakeProvider:
    resultado: ExtractionResult
    name: str = "fake"

    def extract(self, pagina: PaginaDocumento, documento_id: str | None = None) -> ExtractionResult:
        return self.resultado


@pytest.fixture
def db_connection() -> Iterator[Connection]:
    engine = get_engine()
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def _session_factory(connection: Connection) -> Callable[[], Session]:
    def factory() -> Session:
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    return factory


def _pagina() -> SimplePage:
    return SimplePage(numero=1, conteudo=b"fake-bytes", content_type="image/png")


def _fake_resultado(**campos_overrides: object) -> ExtractionResult:
    campos = EvolucaoProntuario(carimbo_legivel=False, **campos_overrides)  # type: ignore[arg-type]
    return ExtractionResult(
        campos=campos,
        confianca=0.9,
        confianca_por_campo={},
        raw_response={},
        modelo="modelo-teste",
        provider="fake",
    )


def test_dispatch_encadeia_validacao_e_grava_reprovacao(db_connection: Connection) -> None:
    session_setup = Session(bind=db_connection, join_transaction_mode="create_savepoint")
    operadora = Operadora(nome="Operadora Teste", codigo=f"TESTE-{uuid.uuid4()}")
    session_setup.add(operadora)
    session_setup.flush()

    regra = Regra(
        operadora_id=operadora.id,
        campo="carimbo_legivel",
        condicao=json.dumps({"tipo": "verdadeiro"}),
        acao="rejeitar",
        motivo_glosa="Carimbo ilegível",
    )
    session_setup.add(regra)
    session_setup.flush()

    documento = Documento(
        operadora_id=operadora.id,
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url="s3://bucket/doc.pdf",
        competencia="2024-03",
        status=DocumentoStatus.PROCESSANDO,
    )
    session_setup.add(documento)
    session_setup.flush()
    session_setup.commit()

    documento_id = documento.id
    regra_id = regra.id

    provider = _FakeProvider(resultado=_fake_resultado())
    dispatcher = SyncExtractionDispatcher(
        provider=provider, session_factory=_session_factory(db_connection)
    )

    dispatcher.dispatch(documento_id, _pagina())

    session_assert = Session(bind=db_connection, join_transaction_mode="create_savepoint")
    validacoes = list(
        session_assert.scalars(select(Validacao).where(Validacao.documento_id == documento_id))
    )
    assert len(validacoes) == 1
    assert validacoes[0].regra_id == regra_id
    assert validacoes[0].resultado is ResultadoValidacao.REPROVADO


def test_dispatch_pula_validacao_silenciosamente_sem_operadora(
    db_connection: Connection,
) -> None:
    session_setup = Session(bind=db_connection, join_transaction_mode="create_savepoint")
    documento = Documento(
        operadora_id=None,
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url="s3://bucket/doc.pdf",
        competencia="2024-03",
        status=DocumentoStatus.PROCESSANDO,
    )
    session_setup.add(documento)
    session_setup.flush()
    session_setup.commit()

    documento_id = documento.id

    provider = _FakeProvider(resultado=_fake_resultado())
    dispatcher = SyncExtractionDispatcher(
        provider=provider, session_factory=_session_factory(db_connection)
    )

    dispatcher.dispatch(documento_id, _pagina())

    session_assert = Session(bind=db_connection, join_transaction_mode="create_savepoint")
    validacoes = list(
        session_assert.scalars(select(Validacao).where(Validacao.documento_id == documento_id))
    )
    assert validacoes == []
