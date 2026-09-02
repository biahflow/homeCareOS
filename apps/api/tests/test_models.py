"""Testes de round-trip dos models SQLAlchemy contra um Postgres real.

Cada teste roda dentro de uma transação que é sempre revertida no fim — não
deixa dado no Postgres compartilhado por outras trilhas (`localhost:5434`).
Pressupõe que `alembic upgrade head` já rodou (ver comandos de verificação
do handoff da Trilha A).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    Extracao,
    LogConferencia,
    Modalidade,
    Operadora,
    Paciente,
    Pendencia,
    PendenciaStatus,
    Regra,
    ResultadoValidacao,
    TipoDocumento,
    Validacao,
)
from homecareos.db.session import get_engine


@pytest.fixture
def db_session() -> Iterator[Session]:
    """Sessão isolada numa transação sempre revertida no fim do teste."""
    engine = get_engine()
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _make_operadora(session: Session, codigo: str = "TESTE") -> Operadora:
    operadora = Operadora(nome="Operadora Teste", codigo=codigo)
    session.add(operadora)
    session.flush()
    return operadora


def _make_documento(
    session: Session,
    *,
    status: DocumentoStatus = DocumentoStatus.PROCESSANDO,
    idempotency_key: str | None = None,
) -> Documento:
    documento = Documento(
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url="s3://bucket/doc.pdf",
        competencia="2026-08",
        status=status,
        idempotency_key=idempotency_key,
    )
    session.add(documento)
    session.flush()
    return documento


def test_operadora_round_trip(db_session: Session) -> None:
    operadora = Operadora(nome="Amil", codigo="AMIL-TESTE", config={"regra_matching": "cpf"})
    db_session.add(operadora)
    db_session.flush()

    loaded = db_session.get(Operadora, operadora.id)

    assert loaded is not None
    assert loaded.nome == "Amil"
    assert loaded.codigo == "AMIL-TESTE"
    assert loaded.config == {"regra_matching": "cpf"}
    assert loaded.created_at is not None


def test_operadora_config_e_opcional(db_session: Session) -> None:
    """Seed inicial só popula nome/código — `config` precisa aceitar NULL."""
    operadora = Operadora(nome="Sem config ainda", codigo="SEMCONFIG")
    db_session.add(operadora)
    db_session.flush()

    loaded = db_session.get(Operadora, operadora.id)

    assert loaded is not None
    assert loaded.config is None


def test_paciente_round_trip(db_session: Session) -> None:
    operadora = _make_operadora(db_session)
    paciente = Paciente(nome="Fulano de Tal", operadora_id=operadora.id, modalidade=Modalidade.AD)
    db_session.add(paciente)
    db_session.flush()

    loaded = db_session.get(Paciente, paciente.id)

    assert loaded is not None
    assert loaded.modalidade is Modalidade.AD
    assert loaded.operadora_id == operadora.id
    assert loaded.data_vencimento_pad is None


def test_documento_paciente_e_operadora_podem_ser_nulos(db_session: Session) -> None:
    documento = _make_documento(db_session, idempotency_key=f"key-{uuid.uuid4()}")

    loaded = db_session.get(Documento, documento.id)

    assert loaded is not None
    assert loaded.paciente_id is None
    assert loaded.operadora_id is None
    assert loaded.status is DocumentoStatus.PROCESSANDO


def test_extracao_nao_guarda_raw_response_no_banco(db_session: Session) -> None:
    """Desvio consciente da issue #2: sem coluna `raw_response`, só a `_ref`.

    O raw response de extração vai pro S3/MinIO; o Postgres guarda só a
    chave do objeto em `raw_response_ref`.
    """
    assert not hasattr(Extracao, "raw_response")

    documento = _make_documento(db_session)
    extracao = Extracao(
        documento_id=documento.id,
        campos_extraidos={"paciente": "Fulano"},
        confianca=0.92,
        confianca_por_campo={"paciente": 0.92},
        raw_response_ref="extracoes/abc123/deadbeef.json",
        modelo="claude-opus-5",
        provider="anthropic",
    )
    db_session.add(extracao)
    db_session.flush()

    loaded = db_session.get(Extracao, extracao.id)
    assert loaded is not None
    assert loaded.raw_response_ref == "extracoes/abc123/deadbeef.json"


def test_extracao_raw_response_ref_e_opcional(db_session: Session) -> None:
    documento = _make_documento(db_session)
    extracao = Extracao(
        documento_id=documento.id,
        campos_extraidos={},
        confianca=0.5,
        confianca_por_campo={},
        modelo="claude-opus-5",
        provider="anthropic",
    )
    db_session.add(extracao)
    db_session.flush()

    loaded = db_session.get(Extracao, extracao.id)
    assert loaded is not None
    assert loaded.raw_response_ref is None


def test_regra_e_validacao_round_trip(db_session: Session) -> None:
    operadora = _make_operadora(db_session)
    documento = _make_documento(db_session)
    regra = Regra(
        operadora_id=operadora.id,
        campo="data_visita",
        condicao="obrigatorio",
        acao="glosar",
        motivo_glosa="Data de visita ausente",
    )
    db_session.add(regra)
    db_session.flush()

    validacao = Validacao(
        documento_id=documento.id,
        regra_id=regra.id,
        resultado=ResultadoValidacao.REPROVADO,
        detalhe="Campo `data_visita` ausente no documento",
    )
    db_session.add(validacao)
    db_session.flush()

    assert regra.ativo is True
    loaded = db_session.get(Validacao, validacao.id)
    assert loaded is not None
    assert loaded.resultado is ResultadoValidacao.REPROVADO


def test_pendencia_resolved_at_so_preenche_na_resolucao(db_session: Session) -> None:
    documento = _make_documento(db_session, status=DocumentoStatus.PROBLEMA)
    pendencia = Pendencia(
        documento_id=documento.id,
        tipo_problema="campo_ausente",
        descricao="Data de visita ausente",
        responsavel="equipe-conferencia",
        status=PendenciaStatus.ABERTA,
        deadline=datetime.now(UTC) + timedelta(days=2),
    )
    db_session.add(pendencia)
    db_session.flush()

    assert pendencia.resolved_at is None

    pendencia.status = PendenciaStatus.RESOLVIDA
    pendencia.resolved_at = datetime.now(UTC)
    db_session.flush()
    db_session.expire(pendencia)

    loaded = db_session.get(Pendencia, pendencia.id)
    assert loaded is not None
    assert loaded.status is PendenciaStatus.RESOLVIDA
    assert loaded.resolved_at is not None


def test_log_conferencia_round_trip(db_session: Session) -> None:
    documento = _make_documento(db_session)
    log = LogConferencia(
        documento_id=documento.id,
        acao="classificado",
        usuario="sistema",
        detalhe="Documento classificado automaticamente",
    )
    db_session.add(log)
    db_session.flush()

    loaded = db_session.get(LogConferencia, log.id)
    assert loaded is not None
    assert loaded.acao == "classificado"


@pytest.mark.parametrize(
    "ciclo",
    [
        pytest.param([DocumentoStatus.PROCESSANDO, DocumentoStatus.APROVADO], id="aprovado-direto"),
        pytest.param(
            [
                DocumentoStatus.PROCESSANDO,
                DocumentoStatus.PROBLEMA,
                DocumentoStatus.EM_CORRECAO,
                DocumentoStatus.RESOLVIDO,
                DocumentoStatus.LIBERADO,
            ],
            id="problema-ate-liberado",
        ),
        pytest.param(
            [
                DocumentoStatus.PROCESSANDO,
                DocumentoStatus.INCOMPLETO,
                DocumentoStatus.EM_CORRECAO,
                DocumentoStatus.RESOLVIDO,
                DocumentoStatus.LIBERADO,
            ],
            id="incompleto-ate-liberado",
        ),
    ],
)
def test_ciclo_completo_de_status_do_documento(
    db_session: Session, ciclo: list[DocumentoStatus]
) -> None:
    """Grava e lê cada passo dos ciclos válidos documentados em `DocumentoStatus`."""
    documento = _make_documento(db_session, status=ciclo[0])

    for status in ciclo:
        documento.status = status
        db_session.flush()
        db_session.expire(documento)

        loaded = db_session.get(Documento, documento.id)
        assert loaded is not None
        assert loaded.status is status
