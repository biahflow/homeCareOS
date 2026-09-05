"""Testes de integração de `GET/PATCH /api/pendencias` e `GET
/api/pendencias/resumo` — contra Postgres real (localhost:5434).

Dois grupos de teste convivem aqui. Os de listagem/resumo criam pendências via
ORM direto (não existe endpoint de criação) sobre um documento-âncora. Os do
ciclo de correção (issue #7) partem de uma classificação de verdade — operadora,
regra ativa e extração — e exercitam `aberta -> em_correcao -> resolvida` com a
propagação para o documento e a revalidação automática.

O banco é compartilhado com o desenvolvimento: cada fixture apaga tudo o que
criou, e nenhuma asserção conta linhas sem filtrar pelos próprios registros.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
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
)
from homecareos.db.session import get_sessionmaker
from homecareos.extraction.schema import EvolucaoProntuario
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY, TEST_API_KEY_PAPEIS

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
COMPETENCIA_TESTE = "2099-03"


def _postgres_responde(settings: Settings) -> str | None:
    try:
        engine = create_engine(
            settings.database_url, connect_args={"connect_timeout": SONDA_TIMEOUT}
        )
        try:
            with engine.connect() as conexao:
                conexao.execute(text("select 1"))
        finally:
            engine.dispose()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


@pytest.fixture(scope="module")
def settings() -> Settings:
    resolved = get_settings()
    motivo = _postgres_responde(resolved)
    if motivo is not None:
        pytest.skip(f"Postgres indisponível em {resolved.database_url}: {motivo}")
    return resolved


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"api_keys": TEST_API_KEY, "api_key_papeis": TEST_API_KEY_PAPEIS}
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def operadora_id(sessao: Session) -> Iterator[uuid.UUID]:
    """Operadora exclusiva do teste — nunca uma operadora do seed (`UNIMED`).

    Uma operadora seedada é compartilhada com o banco de desenvolvimento: os
    testes que filtram por `operadora_id` e comparam o total com o tamanho da
    própria fixture (`test_listar_pendencias_filtra_por_operadora_traz_todas` e
    vizinhos) quebram com qualquer pendência real daquela operadora, contra o
    que o docstring do módulo promete. Criar uma operadora só do teste — igual
    já faz a fixture `ciclo` mais abaixo — mantém o filtro por operadora um
    recorte fechado sem precisar enfraquecer as asserções.
    """
    operadora = Operadora(nome="Operadora Pendências Teste", codigo=f"PEND-{uuid.uuid4()}")
    sessao.add(operadora)
    sessao.commit()

    yield operadora.id

    sessao.execute(text("delete from operadoras where id = :id"), {"id": operadora.id})
    sessao.commit()


@pytest.fixture
def documento_ancora(sessao: Session, operadora_id: uuid.UUID) -> Iterator[Documento]:
    documento = Documento(
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url="s3://fake/pendencias-teste/1",
        competencia=COMPETENCIA_TESTE,
        operadora_id=operadora_id,
    )
    sessao.add(documento)
    sessao.commit()

    yield documento

    _limpar_documentos(sessao, [documento.id])
    sessao.commit()


def _limpar_documentos(sessao: Session, ids: list[uuid.UUID]) -> None:
    """Apaga tudo o que pende de `ids`, na ordem que respeita as FKs."""
    for tabela in ("pendencias", "validacoes", "extracoes", "log_conferencia"):
        sessao.execute(text(f"delete from {tabela} where documento_id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from documentos where id = any(:ids)"), {"ids": ids})


@pytest.fixture
def pendencias_de_teste(sessao: Session, documento_ancora: Documento) -> list[Pendencia]:
    agora = datetime.now(UTC)
    pendencias = [
        Pendencia(
            documento_id=documento_ancora.id,
            tipo_problema="campo_ausente",
            descricao="falta assinatura do responsável técnico",
            responsavel="equipe-conferencia",
            status=PendenciaStatus.ABERTA,
            deadline=agora - timedelta(days=1),
        ),
        Pendencia(
            documento_id=documento_ancora.id,
            tipo_problema="campo_ausente",
            descricao="falta data de atendimento",
            responsavel="equipe-conferencia",
            status=PendenciaStatus.EM_CORRECAO,
            deadline=agora + timedelta(days=3),
        ),
        Pendencia(
            documento_id=documento_ancora.id,
            tipo_problema="campo_ausente",
            descricao="falta CRM do profissional",
            responsavel="equipe-conferencia",
            status=PendenciaStatus.RESOLVIDA,
            deadline=agora + timedelta(days=100),
            resolved_at=agora,
        ),
        Pendencia(
            documento_id=documento_ancora.id,
            tipo_problema="campo_ausente",
            descricao="falta anexo do laudo",
            responsavel="equipe-conferencia",
            status=PendenciaStatus.EM_CORRECAO,
            deadline=agora + timedelta(days=30),
        ),
    ]
    sessao.add_all(pendencias)
    sessao.commit()
    return pendencias


# --- autenticação -------------------------------------------------------------


def test_listar_pendencias_sem_x_api_key_responde_401(api: TestClient) -> None:
    assert api.get("/api/pendencias").status_code == 401


def test_resumo_pendencias_sem_x_api_key_responde_401(api: TestClient) -> None:
    assert api.get("/api/pendencias/resumo").status_code == 401


# --- AC5: listagem pagina e filtra -------------------------------------------


def test_listar_pendencias_filtra_por_status_e_operadora(
    api: TestClient, pendencias_de_teste: list[Pendencia], operadora_id: uuid.UUID
) -> None:
    resposta = api.get(
        f"/api/pendencias?status=aberta&operadora_id={operadora_id}", headers=AUTH_HEADERS
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["paginacao"]["total"] == 1
    assert corpo["data"][0]["status"] == "aberta"


def test_listar_pendencias_filtra_por_deadline(
    api: TestClient, pendencias_de_teste: list[Pendencia], operadora_id: uuid.UUID
) -> None:
    """`deadline` filtra pendências com deadline até (inclusive) a data informada."""
    hoje = datetime.now(UTC).date().isoformat()

    resposta = api.get(
        f"/api/pendencias?deadline={hoje}&operadora_id={operadora_id}", headers=AUTH_HEADERS
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["paginacao"]["total"] == 1
    assert corpo["data"][0]["status"] == "aberta"


def test_listar_pendencias_filtra_por_operadora_traz_todas(
    api: TestClient, pendencias_de_teste: list[Pendencia], operadora_id: uuid.UUID
) -> None:
    resposta = api.get(f"/api/pendencias?operadora_id={operadora_id}", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    assert resposta.json()["paginacao"]["total"] == len(pendencias_de_teste)


# --- AC6: transição de status -------------------------------------------------


def test_transicao_aberta_para_em_correcao_e_valida(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    aberta = next(p for p in pendencias_de_teste if p.status == PendenciaStatus.ABERTA)

    resposta = api.patch(
        f"/api/pendencias/{aberta.id}", json={"status": "em_correcao"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 200
    assert resposta.json()["status"] == "em_correcao"


def test_transicao_em_correcao_para_resolvida_preenche_resolved_at(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    em_correcao = next(p for p in pendencias_de_teste if p.status == PendenciaStatus.EM_CORRECAO)

    resposta = api.patch(
        f"/api/pendencias/{em_correcao.id}", json={"status": "resolvida"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == "resolvida"
    assert corpo["resolved_at"] is not None


def test_transicao_pulando_etapa_responde_422(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    aberta = next(p for p in pendencias_de_teste if p.status == PendenciaStatus.ABERTA)

    resposta = api.patch(
        f"/api/pendencias/{aberta.id}", json={"status": "resolvida"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 422


def test_transicao_para_tras_responde_422(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    resolvida = next(p for p in pendencias_de_teste if p.status == PendenciaStatus.RESOLVIDA)

    resposta = api.patch(
        f"/api/pendencias/{resolvida.id}", json={"status": "aberta"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 422


def test_atualizar_pendencia_inexistente_responde_404(api: TestClient) -> None:
    resposta = api.patch(
        f"/api/pendencias/{uuid.uuid4()}", json={"status": "em_correcao"}, headers=AUTH_HEADERS
    )

    assert resposta.status_code == 404


# --- resumo por status e faixa de deadline ------------------------------------


def test_resumo_pendencias_conta_por_status_e_faixa_deadline(
    api: TestClient, pendencias_de_teste: list[Pendencia]
) -> None:
    resposta = api.get("/api/pendencias/resumo", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert set(corpo["por_status"]) == {"aberta", "em_correcao", "resolvida"}
    assert corpo["por_status"]["aberta"] >= 1
    assert corpo["por_status"]["em_correcao"] >= 1
    assert corpo["por_status"]["resolvida"] >= 1
    assert set(corpo["por_faixa_deadline"]) == {"vencidas", "proximos_7_dias", "futuras"}
    assert corpo["por_faixa_deadline"]["vencidas"] >= 1
    assert corpo["por_faixa_deadline"]["proximos_7_dias"] >= 1
    assert corpo["por_faixa_deadline"]["futuras"] >= 1


# --- issue #7: ciclo de correção propagado para o documento -------------------


@dataclass
class Ciclo:
    """Um documento já classificado, com a pendência que a classificação abriu."""

    operadora: Operadora
    documento: Documento
    pendencia: Pendencia


def _extracao(
    sessao: Session,
    documento_id: uuid.UUID,
    *,
    carimbo_legivel: bool,
    created_at: datetime | None = None,
) -> Extracao:
    extracao = Extracao(
        documento_id=documento_id,
        campos_extraidos=EvolucaoProntuario(carimbo_legivel=carimbo_legivel).model_dump(
            mode="json"
        ),
        confianca=0.9,
        confianca_por_campo={},
        modelo="modelo-teste",
        provider="teste",
    )
    if created_at is not None:
        extracao.created_at = created_at
    sessao.add(extracao)
    sessao.commit()
    return extracao


@pytest.fixture
def ciclo(api: TestClient, sessao: Session) -> Iterator[Ciclo]:
    """Documento reprovado por uma regra `rejeitar`, classificado via `POST /revalidar`.

    A pendência não é inserida à mão de propósito: quem a cria é a
    classificação, e é isso que os testes deste bloco precisam exercitar.
    """
    operadora = Operadora(nome="Operadora Ciclo", codigo=f"CICLO-{uuid.uuid4()}")
    sessao.add(operadora)
    sessao.flush()
    sessao.add(
        Regra(
            operadora_id=operadora.id,
            campo="carimbo_legivel",
            condicao=json.dumps({"tipo": "verdadeiro"}),
            acao="sinalizar",
            motivo_glosa="Carimbo ilegível",
        )
    )
    documento = Documento(
        operadora_id=operadora.id,
        tipo=TipoDocumento.EVOLUCAO,
        arquivo_url="s3://fake/pendencias-ciclo",
        competencia=COMPETENCIA_TESTE,
        status=DocumentoStatus.PROCESSANDO,
    )
    sessao.add(documento)
    sessao.commit()

    _extracao(sessao, documento.id, carimbo_legivel=False)
    resposta = api.post(f"/api/documentos/{documento.id}/revalidar", headers=AUTH_HEADERS)
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "problema"

    pendencia = sessao.scalars(
        select(Pendencia).where(Pendencia.documento_id == documento.id)
    ).one()

    yield Ciclo(operadora=operadora, documento=documento, pendencia=pendencia)

    _limpar_documentos(sessao, [documento.id])
    sessao.execute(text("delete from regras where operadora_id = :id"), {"id": operadora.id})
    sessao.execute(text("delete from operadoras where id = :id"), {"id": operadora.id})
    sessao.commit()


def _status_do_documento(sessao: Session, documento_id: uuid.UUID) -> DocumentoStatus:
    sessao.expire_all()
    documento = sessao.get(Documento, documento_id)
    assert documento is not None
    return documento.status


def _acoes_de_log(sessao: Session, documento_id: uuid.UUID) -> list[str]:
    sessao.expire_all()
    return [
        log.acao
        for log in sessao.scalars(
            select(LogConferencia)
            .where(LogConferencia.documento_id == documento_id)
            .order_by(LogConferencia.created_at)
        )
    ]


def test_classificacao_abre_pendencia_com_deadline_e_responsavel(
    sessao: Session, ciclo: Ciclo
) -> None:
    """AC: pendência criada com deadline quando o documento vira `problema`."""
    assert ciclo.pendencia.status is PendenciaStatus.ABERTA
    assert ciclo.pendencia.tipo_problema == "campo_invalido"
    assert ciclo.pendencia.responsavel == get_settings().pendencia_responsavel_padrao
    # Competência 2099-03 + dia_envio padrão (10) => 2099-04-10, fim do dia.
    assert ciclo.pendencia.deadline == datetime(2099, 4, 10, 23, 59, 59, tzinfo=UTC)
    assert _status_do_documento(sessao, ciclo.documento.id) is DocumentoStatus.PROBLEMA


def test_listagem_expoe_o_campo_que_originou_a_pendencia(api: TestClient, ciclo: Ciclo) -> None:
    """`campo` é aditivo no contrato e é a chave que a revalidação usa para reconciliar."""
    resposta = api.get(f"/api/pendencias?operadora_id={ciclo.operadora.id}", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    (item,) = resposta.json()["data"]
    assert item["campo"] == "carimbo_legivel"


def test_pendencia_em_correcao_leva_o_documento_para_em_correcao(
    api: TestClient, sessao: Session, ciclo: Ciclo
) -> None:
    resposta = api.patch(
        f"/api/pendencias/{ciclo.pendencia.id}",
        json={"status": "em_correcao"},
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 200
    assert _status_do_documento(sessao, ciclo.documento.id) is DocumentoStatus.EM_CORRECAO
    assert "transicao:problema->em_correcao" in _acoes_de_log(sessao, ciclo.documento.id)


def test_patch_reatribui_o_responsavel_da_pendencia(
    api: TestClient, sessao: Session, ciclo: Ciclo
) -> None:
    resposta = api.patch(
        f"/api/pendencias/{ciclo.pendencia.id}",
        json={"status": "em_correcao", "responsavel": "ana.enfermagem"},
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 200
    assert resposta.json()["responsavel"] == "ana.enfermagem"


def test_ciclo_completo_problema_correcao_revalidacao_liberado(
    api: TestClient, sessao: Session, ciclo: Ciclo
) -> None:
    """AC: `problema -> pendência -> correção -> revalidação -> liberado`."""
    api.patch(
        f"/api/pendencias/{ciclo.pendencia.id}",
        json={"status": "em_correcao"},
        headers=AUTH_HEADERS,
    )
    # A correção real chega como uma extração nova; a revalidação lê a última.
    _extracao(
        sessao,
        ciclo.documento.id,
        carimbo_legivel=True,
        created_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    resposta = api.patch(
        f"/api/pendencias/{ciclo.pendencia.id}",
        json={"status": "resolvida"},
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 200
    assert resposta.json()["status"] == "resolvida"
    assert _status_do_documento(sessao, ciclo.documento.id) is DocumentoStatus.LIBERADO
    acoes = _acoes_de_log(sessao, ciclo.documento.id)
    assert acoes == [
        "transicao:processando->problema",
        "transicao:problema->em_correcao",
        "transicao:em_correcao->resolvido",
        "transicao:resolvido->liberado",
    ]


def test_revalidacao_que_reprova_de_novo_reabre_o_bucket_com_pendencia_nova(
    api: TestClient, sessao: Session, ciclo: Ciclo
) -> None:
    """A extração continua reprovando: o documento não pode ficar preso em `resolvido`."""
    api.patch(
        f"/api/pendencias/{ciclo.pendencia.id}",
        json={"status": "em_correcao"},
        headers=AUTH_HEADERS,
    )

    resposta = api.patch(
        f"/api/pendencias/{ciclo.pendencia.id}",
        json={"status": "resolvida"},
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 200
    assert _status_do_documento(sessao, ciclo.documento.id) is DocumentoStatus.PROBLEMA
    abertas = list(
        sessao.scalars(
            select(Pendencia).where(
                Pendencia.documento_id == ciclo.documento.id,
                Pendencia.status != PendenciaStatus.RESOLVIDA,
            )
        )
    )
    assert len(abertas) == 1
    assert abertas[0].id != ciclo.pendencia.id


def test_revalidacao_indisponivel_deixa_o_documento_em_resolvido_e_responde_200(
    api: TestClient, sessao: Session, ciclo: Ciclo
) -> None:
    """Falha da revalidação automática não pode desfazer a transição do usuário."""
    api.patch(
        f"/api/pendencias/{ciclo.pendencia.id}",
        json={"status": "em_correcao"},
        headers=AUTH_HEADERS,
    )
    # Sem regra ativa não há contra o que revalidar.
    sessao.execute(
        text("update regras set ativo = false where operadora_id = :id"),
        {"id": ciclo.operadora.id},
    )
    sessao.commit()

    resposta = api.patch(
        f"/api/pendencias/{ciclo.pendencia.id}",
        json={"status": "resolvida"},
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 200
    assert _status_do_documento(sessao, ciclo.documento.id) is DocumentoStatus.RESOLVIDO
    assert "revalidacao:indisponivel" in _acoes_de_log(sessao, ciclo.documento.id)
