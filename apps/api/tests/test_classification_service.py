"""Testes de integração de `classification.service` contra o Postgres real.

Todo o arquivo roda dentro de uma `Connection` com transação revertida no
teardown, e cada `Session` usa `join_transaction_mode="create_savepoint"` pelo
mesmo motivo de `tests/test_rules_router.py` e
`tests/test_extraction_dispatcher_rules_chain.py`: `classificar_documento` e
`registrar_validacoes` commitam sozinhos, e sem o savepoint esse commit interno
finalizaria a transação externa — os dados vazariam para o banco compartilhado
(`localhost:5434`) em vez de sumirem no rollback.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Connection, func, select
from sqlalchemy.orm import Session

from homecareos.classification.engine import calcular_deadline
from homecareos.classification.errors import (
    DocumentoNaoEncontradoError,
    RevalidacaoIndisponivelError,
    TransicaoInvalidaError,
)
from homecareos.classification.service import (
    classificar_documento,
    revalidar_documento,
    transicionar,
)
from homecareos.config import get_settings
from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    Extracao,
    LogConferencia,
    Operadora,
    Pendencia,
    PendenciaStatus,
    Regra,
    TipoDocumento,
    Validacao,
)
from homecareos.db.session import get_engine
from homecareos.extraction.schema import EvolucaoProntuario
from homecareos.rules.engine import validar

pytestmark = pytest.mark.integration

COMPETENCIA_TESTE = "2099-03"
DIA_ENVIO_TESTE = 7


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


@pytest.fixture
def sessao(db_connection: Connection) -> Iterator[Session]:
    session = Session(bind=db_connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def operadora(sessao: Session) -> Operadora:
    operadora = Operadora(
        nome="Operadora Teste Classificação",
        codigo=f"CLASSIF-{uuid.uuid4()}",
        dia_envio=DIA_ENVIO_TESTE,
    )
    sessao.add(operadora)
    sessao.flush()
    return operadora


def _documento(
    sessao: Session,
    operadora: Operadora | None,
    *,
    competencia: str = COMPETENCIA_TESTE,
    status: DocumentoStatus = DocumentoStatus.PROCESSANDO,
) -> Documento:
    documento = Documento(
        operadora_id=operadora.id if operadora is not None else None,
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url="s3://fake/classificacao-teste",
        competencia=competencia,
        status=status,
    )
    sessao.add(documento)
    sessao.flush()
    return documento


def _regra(operadora: Operadora, *, campo: str, acao: str, motivo_glosa: str) -> Regra:
    return Regra(
        operadora_id=operadora.id,
        campo=campo,
        condicao=json.dumps({"tipo": "verdadeiro"}),
        acao=acao,
        motivo_glosa=motivo_glosa,
    )


def _campos(**overrides: object) -> EvolucaoProntuario:
    return EvolucaoProntuario(**overrides)  # type: ignore[arg-type]


def _logs(sessao: Session, documento_id: uuid.UUID) -> list[LogConferencia]:
    return list(
        sessao.scalars(
            select(LogConferencia)
            .where(LogConferencia.documento_id == documento_id)
            .order_by(LogConferencia.created_at)
        )
    )


def _pendencias(sessao: Session, documento_id: uuid.UUID) -> list[Pendencia]:
    return list(sessao.scalars(select(Pendencia).where(Pendencia.documento_id == documento_id)))


# --- transicionar -------------------------------------------------------------


def test_transicionar_registra_linha_em_log_conferencia(
    sessao: Session, operadora: Operadora
) -> None:
    """Critério de aceite: toda transição de status do documento é auditável."""
    documento = _documento(sessao, operadora)

    transicionar(
        sessao,
        documento,
        DocumentoStatus.PROBLEMA,
        usuario="teste",
        detalhe="motivo do teste",
    )
    sessao.commit()

    (log,) = _logs(sessao, documento.id)
    assert log.acao == "transicao:processando->problema"
    assert log.usuario == "teste"
    assert log.detalhe == "motivo do teste"
    assert documento.status is DocumentoStatus.PROBLEMA


def test_transicao_fora_do_mapa_levanta_e_nao_muda_o_status(
    sessao: Session, operadora: Operadora
) -> None:
    documento = _documento(sessao, operadora, status=DocumentoStatus.PROCESSANDO)

    with pytest.raises(TransicaoInvalidaError):
        transicionar(
            sessao, documento, DocumentoStatus.LIBERADO, usuario="teste", detalhe="pulo indevido"
        )

    assert documento.status is DocumentoStatus.PROCESSANDO
    assert _logs(sessao, documento.id) == []


def test_status_terminal_nao_transiciona(sessao: Session, operadora: Operadora) -> None:
    documento = _documento(sessao, operadora, status=DocumentoStatus.APROVADO)

    with pytest.raises(TransicaoInvalidaError):
        transicionar(
            sessao, documento, DocumentoStatus.PROBLEMA, usuario="teste", detalhe="reabertura"
        )


def test_resolvido_pode_voltar_para_problema(sessao: Session, operadora: Operadora) -> None:
    """Extensão deliberada do ciclo: a revalidação pode reprovar de novo."""
    documento = _documento(sessao, operadora, status=DocumentoStatus.RESOLVIDO)

    transicionar(
        sessao, documento, DocumentoStatus.PROBLEMA, usuario="teste", detalhe="reprovou de novo"
    )

    assert documento.status is DocumentoStatus.PROBLEMA


def test_problema_pode_reconfirmar_o_proprio_bucket(sessao: Session, operadora: Operadora) -> None:
    """Auto-transição: reconfirmar o bucket é evento real e merece linha de log."""
    documento = _documento(sessao, operadora, status=DocumentoStatus.PROBLEMA)

    transicionar(
        sessao, documento, DocumentoStatus.PROBLEMA, usuario="teste", detalhe="reconfirmado"
    )
    sessao.commit()

    (log,) = _logs(sessao, documento.id)
    assert log.acao == "transicao:problema->problema"


# --- classificar_documento ----------------------------------------------------


def test_classificar_documento_inexistente_levanta(sessao: Session) -> None:
    with pytest.raises(DocumentoNaoEncontradoError):
        classificar_documento(sessao, uuid.uuid4(), [], usuario="teste")


def test_documento_sem_reprovacao_vai_para_aprovado_sem_pendencia(
    sessao: Session, operadora: Operadora
) -> None:
    documento = _documento(sessao, operadora)
    regra = _regra(
        operadora, campo="carimbo_legivel", acao="rejeitar", motivo_glosa="Carimbo ilegível"
    )
    sessao.add(regra)
    sessao.flush()
    resultados = validar(_campos(carimbo_legivel=True), [regra], competencia=COMPETENCIA_TESTE)

    status = classificar_documento(sessao, documento.id, resultados, usuario="teste")

    assert status is DocumentoStatus.APROVADO
    assert documento.status is DocumentoStatus.APROVADO
    assert _pendencias(sessao, documento.id) == []


def test_reprovacao_rejeitar_abre_pendencia_com_todos_os_campos(
    sessao: Session, operadora: Operadora
) -> None:
    documento = _documento(sessao, operadora)
    regra = _regra(
        operadora, campo="carimbo_legivel", acao="rejeitar", motivo_glosa="Carimbo ilegível"
    )
    sessao.add(regra)
    sessao.flush()
    resultados = validar(_campos(carimbo_legivel=False), [regra], competencia=COMPETENCIA_TESTE)

    status = classificar_documento(sessao, documento.id, resultados, usuario="sistema")

    assert status is DocumentoStatus.INCOMPLETO
    (pendencia,) = _pendencias(sessao, documento.id)
    assert pendencia.tipo_problema == "campo_ausente"
    assert pendencia.status is PendenciaStatus.ABERTA
    assert pendencia.responsavel == get_settings().pendencia_responsavel_padrao
    assert pendencia.resolved_at is None
    assert "Carimbo ilegível" in pendencia.descricao
    assert pendencia.deadline == calcular_deadline(COMPETENCIA_TESTE, DIA_ENVIO_TESTE)


def test_reprovacao_sinalizar_abre_pendencia_de_campo_invalido(
    sessao: Session, operadora: Operadora
) -> None:
    documento = _documento(sessao, operadora)
    regra = _regra(
        operadora,
        campo="assinatura_profissional_presente",
        acao="sinalizar",
        motivo_glosa="Assinatura ausente",
    )
    sessao.add(regra)
    sessao.flush()
    resultados = validar(
        _campos(assinatura_profissional_presente=False), [regra], competencia=COMPETENCIA_TESTE
    )

    status = classificar_documento(sessao, documento.id, resultados, usuario="sistema")

    assert status is DocumentoStatus.PROBLEMA
    (pendencia,) = _pendencias(sessao, documento.id)
    assert pendencia.tipo_problema == "campo_invalido"


def test_classificacao_registra_a_transicao_em_log_conferencia(
    sessao: Session, operadora: Operadora
) -> None:
    documento = _documento(sessao, operadora)
    regra = _regra(
        operadora, campo="carimbo_legivel", acao="rejeitar", motivo_glosa="Carimbo ilegível"
    )
    sessao.add(regra)
    sessao.flush()
    resultados = validar(_campos(carimbo_legivel=False), [regra], competencia=COMPETENCIA_TESTE)

    classificar_documento(sessao, documento.id, resultados, usuario="sistema")

    (log,) = _logs(sessao, documento.id)
    assert log.acao == "transicao:processando->incompleto"
    assert log.usuario == "sistema"


def test_competencia_malformada_usa_fim_do_dia_de_hoje_e_registra_o_motivo(
    sessao: Session, operadora: Operadora
) -> None:
    """Competência inválida não pode derrubar o upload nem gerar pendência sem prazo."""
    documento = _documento(sessao, operadora, competencia="março/2099")
    regra = _regra(
        operadora, campo="carimbo_legivel", acao="rejeitar", motivo_glosa="Carimbo ilegível"
    )
    sessao.add(regra)
    sessao.flush()
    resultados = validar(_campos(carimbo_legivel=False), [regra], competencia="março/2099")

    status = classificar_documento(sessao, documento.id, resultados, usuario="sistema")

    assert status is DocumentoStatus.INCOMPLETO
    (pendencia,) = _pendencias(sessao, documento.id)
    assert pendencia.deadline.astimezone(UTC).date() == datetime.now(UTC).date()
    acoes = {log.acao for log in _logs(sessao, documento.id)}
    assert "deadline:fallback" in acoes


# --- revalidar_documento ------------------------------------------------------


def test_revalidar_documento_inexistente_levanta(sessao: Session) -> None:
    with pytest.raises(DocumentoNaoEncontradoError):
        revalidar_documento(sessao, uuid.uuid4(), usuario="teste")


def test_revalidar_sem_operadora_levanta(sessao: Session) -> None:
    documento = _documento(sessao, None)

    with pytest.raises(RevalidacaoIndisponivelError, match="operadora"):
        revalidar_documento(sessao, documento.id, usuario="teste")


def test_revalidar_sem_extracao_levanta(sessao: Session, operadora: Operadora) -> None:
    documento = _documento(sessao, operadora)

    with pytest.raises(RevalidacaoIndisponivelError, match="extração"):
        revalidar_documento(sessao, documento.id, usuario="teste")


def test_revalidar_sem_regra_ativa_levanta(sessao: Session, operadora: Operadora) -> None:
    documento = _documento(sessao, operadora)
    sessao.add(
        Extracao(
            documento_id=documento.id,
            campos_extraidos=_campos(carimbo_legivel=True).model_dump(mode="json"),
            confianca=0.9,
            confianca_por_campo={},
            modelo="modelo-teste",
            provider="teste",
        )
    )
    sessao.flush()

    with pytest.raises(RevalidacaoIndisponivelError, match="regras ativas"):
        revalidar_documento(sessao, documento.id, usuario="teste")


def test_revalidar_documento_corrigido_libera(sessao: Session, operadora: Operadora) -> None:
    """Ciclo fechado: documento em `resolvido` que agora passa nas regras vira `liberado`."""
    documento = _documento(sessao, operadora, status=DocumentoStatus.RESOLVIDO)
    regra = _regra(
        operadora, campo="carimbo_legivel", acao="rejeitar", motivo_glosa="Carimbo ilegível"
    )
    sessao.add(regra)
    sessao.add(
        Extracao(
            documento_id=documento.id,
            campos_extraidos=_campos(carimbo_legivel=True).model_dump(mode="json"),
            confianca=0.9,
            confianca_por_campo={},
            modelo="modelo-teste",
            provider="teste",
        )
    )
    sessao.flush()

    status = revalidar_documento(sessao, documento.id, usuario="teste")

    assert status is DocumentoStatus.LIBERADO
    assert _pendencias(sessao, documento.id) == []


def test_revalidar_que_reprova_de_novo_reabre_o_bucket(
    sessao: Session, operadora: Operadora
) -> None:
    documento = _documento(sessao, operadora, status=DocumentoStatus.RESOLVIDO)
    regra = _regra(
        operadora, campo="carimbo_legivel", acao="rejeitar", motivo_glosa="Carimbo ilegível"
    )
    sessao.add(regra)
    sessao.add(
        Extracao(
            documento_id=documento.id,
            campos_extraidos=_campos(carimbo_legivel=False).model_dump(mode="json"),
            confianca=0.9,
            confianca_por_campo={},
            modelo="modelo-teste",
            provider="teste",
        )
    )
    sessao.flush()

    status = revalidar_documento(sessao, documento.id, usuario="teste")

    assert status is DocumentoStatus.INCOMPLETO
    assert len(_pendencias(sessao, documento.id)) == 1


def test_revalidar_documento_terminal_levanta_transicao_invalida(
    sessao: Session, operadora: Operadora
) -> None:
    """`liberado` já foi para o faturamento — revalidar não pode reabri-lo em silêncio."""
    documento = _documento(sessao, operadora, status=DocumentoStatus.LIBERADO)
    regra = _regra(
        operadora, campo="carimbo_legivel", acao="rejeitar", motivo_glosa="Carimbo ilegível"
    )
    sessao.add(regra)
    sessao.add(
        Extracao(
            documento_id=documento.id,
            campos_extraidos=_campos(carimbo_legivel=False).model_dump(mode="json"),
            confianca=0.9,
            confianca_por_campo={},
            modelo="modelo-teste",
            provider="teste",
        )
    )
    sessao.flush()

    with pytest.raises(TransicaoInvalidaError):
        revalidar_documento(sessao, documento.id, usuario="teste")


# --- reconciliação de pendências entre revalidações ---------------------------


def _cenario_reprovado(
    sessao: Session, operadora: Operadora, *, campos: list[str]
) -> tuple[Documento, list[Regra]]:
    """Documento em `problema` com uma pendência aberta por campo reprovado."""
    documento = _documento(sessao, operadora)
    regras = [
        _regra(operadora, campo=campo, acao="sinalizar", motivo_glosa=f"Glosa de {campo}")
        for campo in campos
    ]
    sessao.add_all(regras)
    sessao.flush()
    resultados = validar(_campos(), regras, competencia=COMPETENCIA_TESTE)
    classificar_documento(sessao, documento.id, resultados, usuario="sistema")
    return documento, regras


def test_revalidar_o_mesmo_problema_nao_duplica_a_pendencia(
    sessao: Session, operadora: Operadora
) -> None:
    """Revalidação é idempotente: o mesmo problema não vira uma pendência nova a cada chamada."""
    documento, regras = _cenario_reprovado(sessao, operadora, campos=["carimbo_legivel"])
    (original,) = _pendencias(sessao, documento.id)
    identidade = (original.id, original.deadline, original.responsavel)

    for _ in range(2):
        resultados = validar(_campos(), regras, competencia=COMPETENCIA_TESTE)
        classificar_documento(sessao, documento.id, resultados, usuario="sistema")

    (pendencia,) = _pendencias(sessao, documento.id)
    assert (pendencia.id, pendencia.deadline, pendencia.responsavel) == identidade
    assert pendencia.status is PendenciaStatus.ABERTA


def test_problema_que_deixou_de_existir_resolve_a_pendencia_e_libera(
    sessao: Session, operadora: Operadora
) -> None:
    """Regra desativada: a pendência órfã fecha em vez de acompanhar o documento liberado."""
    documento, _ = _cenario_reprovado(sessao, operadora, campos=["carimbo_legivel"])
    (original,) = _pendencias(sessao, documento.id)

    # Nenhuma regra ativa reprova mais este campo.
    status = classificar_documento(sessao, documento.id, [], usuario="sistema")

    assert status is DocumentoStatus.LIBERADO
    (pendencia,) = _pendencias(sessao, documento.id)
    assert pendencia.id == original.id
    assert pendencia.status is PendenciaStatus.RESOLVIDA
    assert pendencia.resolved_at is not None
    acoes = [log.acao for log in _logs(sessao, documento.id)]
    assert "pendencia:resolvida_por_revalidacao" in acoes


def test_pendencia_em_correcao_sem_regra_correspondente_tambem_resolve(
    sessao: Session, operadora: Operadora
) -> None:
    """`em_correcao` não protege a pendência: o problema sumiu, ela está resolvida."""
    documento, _ = _cenario_reprovado(sessao, operadora, campos=["carimbo_legivel"])
    (original,) = _pendencias(sessao, documento.id)
    original.status = PendenciaStatus.EM_CORRECAO
    transicionar(
        sessao,
        documento,
        DocumentoStatus.EM_CORRECAO,
        usuario="teste",
        detalhe="pendência entrou em correção",
    )
    sessao.commit()

    status = classificar_documento(sessao, documento.id, [], usuario="sistema")

    assert status is DocumentoStatus.LIBERADO
    (pendencia,) = _pendencias(sessao, documento.id)
    assert pendencia.status is PendenciaStatus.RESOLVIDA


def test_documento_em_correcao_que_passa_nas_regras_vai_direto_para_liberado(
    sessao: Session, operadora: Operadora
) -> None:
    """Transição `em_correcao -> liberado`: sem pendência sobrevivente, não há o que aguardar."""
    documento, _ = _cenario_reprovado(sessao, operadora, campos=["carimbo_legivel"])
    transicionar(
        sessao,
        documento,
        DocumentoStatus.EM_CORRECAO,
        usuario="teste",
        detalhe="correção iniciada",
    )
    sessao.commit()

    status = classificar_documento(sessao, documento.id, [], usuario="sistema")

    assert status is DocumentoStatus.LIBERADO
    assert "transicao:em_correcao->liberado" in [log.acao for log in _logs(sessao, documento.id)]


def test_so_a_pendencia_que_parou_de_reprovar_resolve(
    sessao: Session, operadora: Operadora
) -> None:
    documento, regras = _cenario_reprovado(
        sessao, operadora, campos=["carimbo_legivel", "assinatura_profissional_presente"]
    )
    por_campo = {p.campo: p for p in _pendencias(sessao, documento.id)}
    assert set(por_campo) == {"carimbo_legivel", "assinatura_profissional_presente"}
    sobrevivente = por_campo["carimbo_legivel"]
    identidade = (sobrevivente.id, sobrevivente.deadline, sobrevivente.responsavel)

    regra_que_continua = next(r for r in regras if r.campo == "carimbo_legivel")
    resultados = validar(_campos(), [regra_que_continua], competencia=COMPETENCIA_TESTE)
    classificar_documento(sessao, documento.id, resultados, usuario="sistema")

    atualizadas = {p.campo: p for p in _pendencias(sessao, documento.id)}
    assert len(atualizadas) == 2
    assert atualizadas["assinatura_profissional_presente"].status is PendenciaStatus.RESOLVIDA
    viva = atualizadas["carimbo_legivel"]
    assert viva.status is PendenciaStatus.ABERTA
    assert (viva.id, viva.deadline, viva.responsavel) == identidade


def test_pendencia_legada_sem_campo_e_resolvida_pela_revalidacao(
    sessao: Session, operadora: Operadora
) -> None:
    """`campo IS NULL` (pendência anterior à issue #7) não casa com proposta nenhuma."""
    documento, regras = _cenario_reprovado(sessao, operadora, campos=["carimbo_legivel"])
    legada = Pendencia(
        documento_id=documento.id,
        campo=None,
        tipo_problema="campo_invalido",
        descricao="pendência anterior à classificação automática",
        responsavel="equipe-conferencia",
        status=PendenciaStatus.ABERTA,
        deadline=datetime(2099, 4, 10, 23, 59, 59, tzinfo=UTC),
    )
    sessao.add(legada)
    sessao.commit()

    resultados = validar(_campos(), regras, competencia=COMPETENCIA_TESTE)
    classificar_documento(sessao, documento.id, resultados, usuario="sistema")

    sessao.refresh(legada)
    assert legada.status is PendenciaStatus.RESOLVIDA
    assert len(_pendencias(sessao, documento.id)) == 2  # a legada resolvida + a viva


# --- achado 2: requisição recusada não grava `validacoes` ---------------------


def test_revalidar_documento_terminal_nao_grava_validacoes(
    sessao: Session, operadora: Operadora
) -> None:
    """409 é recusa: uma requisição rejeitada não pode deixar linha nova em `validacoes`."""
    documento = _documento(sessao, operadora, status=DocumentoStatus.LIBERADO)
    regra = _regra(
        operadora, campo="carimbo_legivel", acao="rejeitar", motivo_glosa="Carimbo ilegível"
    )
    sessao.add(regra)
    sessao.add(
        Extracao(
            documento_id=documento.id,
            campos_extraidos=_campos(carimbo_legivel=False).model_dump(mode="json"),
            confianca=0.9,
            confianca_por_campo={},
            modelo="modelo-teste",
            provider="teste",
        )
    )
    sessao.flush()
    antes = sessao.scalar(
        select(func.count()).select_from(Validacao).where(Validacao.documento_id == documento.id)
    )

    for _ in range(2):
        with pytest.raises(TransicaoInvalidaError):
            revalidar_documento(sessao, documento.id, usuario="teste")

    depois = sessao.scalar(
        select(func.count()).select_from(Validacao).where(Validacao.documento_id == documento.id)
    )
    assert depois == antes == 0
