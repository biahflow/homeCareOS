"""Testes de integração de `/api/relatorios/*` — contra Postgres real (localhost:5434).

O banco é compartilhado com o desenvolvimento e já tem operadoras seedadas e o
catálogo de regras. Por isso o cenário cria **sempre** a sua própria operadora
(`REL-<uuid>`) e usa competências que ninguém mais usa (`2098-05`/`2098-06`),
toda asserção filtra pelos próprios registros, e o teardown apaga tudo o que o
teste criou. Contar linha do banco inteiro aqui daria um teste que passa hoje e
falha quando outra trilha inserir um documento.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from homecareos.config import Settings, get_settings
from homecareos.db.models import (
    BaselineCompetencia,
    Documento,
    DocumentoStatus,
    Extracao,
    Modalidade,
    Operadora,
    Paciente,
    Pendencia,
    PendenciaStatus,
    TipoDocumento,
)
from homecareos.db.session import get_sessionmaker
from homecareos.main import app
from homecareos.reports import csv_export
from tests.conftest import AUTH_HEADERS, TEST_API_KEY, TEST_API_KEY_PAPEIS

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
COMPETENCIA = "2098-05"
COMPETENCIA_RESOLUCAO = "2098-06"
# Competência exclusiva do baseline consolidado: ele é único por competência na
# tabela inteira (índice parcial), então não dá para isolá-lo por operadora.
COMPETENCIA_CONSOLIDADO = "2098-07"

# Documento fora da janela recente, para exercitar `data_inicio`/`data_fim`.
RECEBIDO_ANTIGO = datetime(2020, 1, 15, 12, 0, tzinfo=UTC)

# Pendência resolvida com exatamente 5 horas entre abertura e resolução.
ABERTA_EM = datetime(2098, 6, 1, 10, 0, tzinfo=UTC)
RESOLVIDA_EM = datetime(2098, 6, 1, 15, 0, tzinfo=UTC)


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


@dataclass
class Cenario:
    """Uma competência inteira montada à mão, com um documento de cada bucket."""

    operadora: Operadora
    paciente: Paciente
    incompleto: Documento
    problema: Documento
    aprovado: Documento
    antigo: Documento
    resolucao: Documento


@pytest.fixture
def cenario(sessao: Session) -> Iterator[Cenario]:
    operadora = Operadora(nome="Operadora Relatórios", codigo=f"REL-{uuid.uuid4()}")
    sessao.add(operadora)
    sessao.flush()
    paciente = Paciente(nome="Joana Ribeiro", operadora_id=operadora.id, modalidade=Modalidade.AD)
    sessao.add(paciente)
    sessao.flush()

    def _documento(
        status: DocumentoStatus,
        *,
        competencia: str = COMPETENCIA,
        created_at: datetime | None = None,
    ) -> Documento:
        documento = Documento(
            tipo=TipoDocumento.EVOLUCAO,
            arquivo_url=f"s3://fake/relatorios/{uuid.uuid4()}",
            competencia=competencia,
            status=status,
            operadora_id=operadora.id,
            paciente_id=paciente.id,
        )
        if created_at is not None:
            documento.created_at = created_at
        sessao.add(documento)
        return documento

    incompleto = _documento(DocumentoStatus.INCOMPLETO)
    problema = _documento(DocumentoStatus.PROBLEMA)
    aprovado = _documento(DocumentoStatus.APROVADO)
    antigo = _documento(DocumentoStatus.PROCESSANDO, created_at=RECEBIDO_ANTIGO)
    resolucao = _documento(DocumentoStatus.LIBERADO, competencia=COMPETENCIA_RESOLUCAO)
    sessao.flush()

    agora = datetime.now(UTC)
    sessao.add_all(
        [
            # Duas pendências abertas no `incompleto`, com `created_at` explícito
            # para a ordem de `problema_encontrado` ser determinística.
            Pendencia(
                documento_id=incompleto.id,
                tipo_problema="campo_ausente",
                descricao="falta assinatura do profissional",
                responsavel="equipe-conferencia",
                status=PendenciaStatus.ABERTA,
                deadline=agora + timedelta(days=3),
                created_at=agora - timedelta(minutes=2),
            ),
            Pendencia(
                documento_id=incompleto.id,
                tipo_problema="campo_ausente",
                descricao="falta data de atendimento",
                responsavel="equipe-conferencia",
                status=PendenciaStatus.ABERTA,
                deadline=agora + timedelta(days=5),
                created_at=agora - timedelta(minutes=1),
            ),
            Pendencia(
                documento_id=problema.id,
                tipo_problema="campo_invalido",
                descricao="carimbo ilegível",
                responsavel="equipe-conferencia",
                status=PendenciaStatus.EM_CORRECAO,
                deadline=agora + timedelta(days=10),
                created_at=agora,
            ),
            # Resolvida, na competência separada: é ela que dá o tempo médio de
            # resolução sem mexer nas contagens da competência principal.
            Pendencia(
                documento_id=resolucao.id,
                tipo_problema="campo_ausente",
                descricao="faltava o COREN",
                responsavel="equipe-conferencia",
                status=PendenciaStatus.RESOLVIDA,
                deadline=RESOLVIDA_EM,
                created_at=ABERTA_EM,
                resolved_at=RESOLVIDA_EM,
            ),
        ]
    )
    sessao.add(
        Extracao(
            documento_id=problema.id,
            campos_extraidos={"data_atendimento": "2098-05-14"},
            confianca=0.9,
            confianca_por_campo={},
            modelo="modelo-teste",
            provider="teste",
        )
    )
    sessao.commit()

    yield Cenario(
        operadora=operadora,
        paciente=paciente,
        incompleto=incompleto,
        problema=problema,
        aprovado=aprovado,
        antigo=antigo,
        resolucao=resolucao,
    )

    ids = [incompleto.id, problema.id, aprovado.id, antigo.id, resolucao.id]
    for tabela in ("pendencias", "validacoes", "extracoes", "log_conferencia"):
        sessao.execute(text(f"delete from {tabela} where documento_id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from documentos where id = any(:ids)"), {"ids": ids})
    sessao.execute(
        text("delete from baselines_competencia where operadora_id = :id"), {"id": operadora.id}
    )
    sessao.execute(text("delete from pacientes where id = :id"), {"id": paciente.id})
    sessao.execute(text("delete from operadoras where id = :id"), {"id": operadora.id})
    sessao.commit()


def _conferencia(api: TestClient, cenario: Cenario, extra: str = "") -> dict[str, object]:
    resposta = api.get(
        f"/api/relatorios/conferencia?operadora_id={cenario.operadora.id}"
        f"&competencia={COMPETENCIA}{extra}",
        headers=AUTH_HEADERS,
    )
    assert resposta.status_code == 200
    corpo: dict[str, object] = resposta.json()
    return corpo


# --- autenticação -------------------------------------------------------------


@pytest.mark.parametrize(
    ("metodo", "rota"),
    [
        ("get", "/api/relatorios/conferencia"),
        ("get", "/api/relatorios/conferencia.csv"),
        ("get", "/api/relatorios/metricas"),
        ("put", "/api/relatorios/baseline"),
        ("get", "/api/relatorios/baseline"),
    ],
)
def test_rotas_sem_x_api_key_respondem_401(api: TestClient, metodo: str, rota: str) -> None:
    """A proteção vem do `include_router` em `main.py`; nenhuma rota pode escapar dela."""
    resposta = api.request(metodo.upper(), rota, json={} if metodo == "put" else None)

    assert resposta.status_code == 401


# --- relatório de conferência -------------------------------------------------


def test_conferencia_traz_as_linhas_da_competencia_com_nomes_e_pendencias(
    api: TestClient, cenario: Cenario
) -> None:
    corpo = _conferencia(api, cenario)

    assert corpo["paginacao"]["total"] == 4  # type: ignore[index]
    linhas = {linha["documento_id"]: linha for linha in corpo["data"]}  # type: ignore[union-attr]

    incompleto = linhas[str(cenario.incompleto.id)]
    assert incompleto["paciente_nome"] == cenario.paciente.nome
    assert incompleto["operadora_nome"] == cenario.operadora.nome
    assert incompleto["severidade"] == "CRITICO"
    assert incompleto["pendencias_abertas"] == 2
    assert incompleto["problema_encontrado"] == (
        "falta assinatura do profissional | falta data de atendimento"
    )
    assert incompleto["acao_necessaria"].startswith(
        "Documento volta para o campo: 2 pendência(s) a corrigir. Prazo:"
    )

    problema = linhas[str(cenario.problema.id)]
    assert problema["severidade"] == "ATENCAO"
    assert problema["pendencias_abertas"] == 1
    assert problema["problema_encontrado"] == "carimbo ilegível"
    assert problema["data_atendimento"] == "2098-05-14"

    aprovado = linhas[str(cenario.aprovado.id)]
    assert aprovado["severidade"] == "OK"
    assert aprovado["pendencias_abertas"] == 0
    assert aprovado["problema_encontrado"] == ""
    assert aprovado["acao_necessaria"] == "Nenhuma."
    assert aprovado["deadline"] is None


def test_conferencia_ordena_pelo_que_precisa_de_acao_humana_primeiro(
    api: TestClient, cenario: Cenario
) -> None:
    """`incompleto` antes de `problema`, e `aprovado` por último — não é o ciclo de vida."""
    corpo = _conferencia(api, cenario)

    ordem = [linha["documento_id"] for linha in corpo["data"]]  # type: ignore[union-attr]
    assert ordem.index(str(cenario.incompleto.id)) < ordem.index(str(cenario.problema.id))
    assert ordem.index(str(cenario.problema.id)) < ordem.index(str(cenario.aprovado.id))


def test_conferencia_apenas_pendentes_exclui_o_documento_sem_pendencia(
    api: TestClient, cenario: Cenario
) -> None:
    corpo = _conferencia(api, cenario, extra="&apenas_pendentes=true")

    ids = {linha["documento_id"] for linha in corpo["data"]}  # type: ignore[union-attr]
    assert ids == {str(cenario.incompleto.id), str(cenario.problema.id)}
    assert corpo["paginacao"]["total"] == 2  # type: ignore[index]


def test_conferencia_filtra_por_status(api: TestClient, cenario: Cenario) -> None:
    corpo = _conferencia(api, cenario, extra="&status=aprovado")

    assert corpo["paginacao"]["total"] == 1  # type: ignore[index]
    assert corpo["data"][0]["documento_id"] == str(cenario.aprovado.id)  # type: ignore[index]


def test_conferencia_com_competencia_invalida_responde_422(api: TestClient) -> None:
    resposta = api.get("/api/relatorios/conferencia?competencia=2026-13", headers=AUTH_HEADERS)

    assert resposta.status_code == 422
    assert "AAAA-MM" in resposta.json()["error"]["mensagem"]


def test_conferencia_filtra_por_janela_de_recebimento(api: TestClient, cenario: Cenario) -> None:
    """`data_inicio`/`data_fim` filtram `created_at`, inclusive nas duas pontas."""
    corpo = _conferencia(api, cenario, extra="&data_inicio=2020-01-15&data_fim=2020-01-15")

    assert corpo["paginacao"]["total"] == 1  # type: ignore[index]
    assert corpo["data"][0]["documento_id"] == str(cenario.antigo.id)  # type: ignore[index]


def test_conferencia_janela_que_exclui_o_documento_antigo(
    api: TestClient, cenario: Cenario
) -> None:
    corpo = _conferencia(api, cenario, extra="&data_inicio=2020-01-16")

    ids = {linha["documento_id"] for linha in corpo["data"]}  # type: ignore[union-attr]
    assert str(cenario.antigo.id) not in ids


# --- CSV ----------------------------------------------------------------------


def test_conferencia_csv_abre_no_excel_e_bate_com_o_json(api: TestClient, cenario: Cenario) -> None:
    filtro = f"operadora_id={cenario.operadora.id}&competencia={COMPETENCIA}"

    resposta = api.get(f"/api/relatorios/conferencia.csv?{filtro}", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/csv")
    disposicao = resposta.headers["content-disposition"]
    assert "attachment" in disposicao
    assert f"conferencia-{COMPETENCIA}-" in disposicao

    corpo = resposta.content.decode("utf-8")
    assert corpo.startswith(csv_export.BOM_UTF8)

    linhas = list(
        csv.reader(
            io.StringIO(corpo.removeprefix(csv_export.BOM_UTF8)),
            delimiter=csv_export.DELIMITADOR,
        )
    )
    assert linhas[0] == list(csv_export.CABECALHO)

    esperado = _conferencia(api, cenario)["paginacao"]["total"]  # type: ignore[index]
    assert len(linhas) - 1 == esperado


# --- baseline -----------------------------------------------------------------


def _corpo_baseline(cenario: Cenario, **overrides: object) -> dict[str, object]:
    corpo: dict[str, object] = {
        "competencia": COMPETENCIA,
        "operadora_id": str(cenario.operadora.id),
        "documentos_enviados": 100,
        "documentos_glosados": 20,
        "valor_glosado_centavos": 1_234_500,
        "horas_conferencia": 40.0,
        "fonte": "demonstrativo da operadora",
    }
    corpo.update(overrides)
    return corpo


@pytest.fixture
def limpar_consolidado(sessao: Session) -> Iterator[None]:
    """Apaga o baseline consolidado da competência do teste, antes e depois.

    O consolidado tem `operadora_id IS NULL` e é único por competência **na
    tabela inteira** (índice parcial), então ele não pode ser isolado por
    operadora como os demais registros deste módulo.
    """
    yield
    sessao.execute(
        text("delete from baselines_competencia where competencia = :c and operadora_id is null"),
        {"c": COMPETENCIA_CONSOLIDADO},
    )
    sessao.commit()


def test_put_baseline_consolidado_atualiza_em_vez_de_estourar_no_indice_parcial(
    api: TestClient, sessao: Session, limpar_consolidado: None
) -> None:
    """Regressão do caminho `operadora_id IS NULL`, que casa com o índice PARCIAL.

    O upsert é um `INSERT ... ON CONFLICT DO UPDATE`, e o alvo do `ON CONFLICT`
    precisa ser o índice parcial neste caso: apontar para
    `(competencia, operadora_id)` faria o consolidado escapar da cláusula
    (dois `NULL` não colidem no índice comum) e o segundo `PUT` estouraria no
    índice parcial com 500 em vez de corrigir o número.
    """
    corpo = {
        "competencia": COMPETENCIA_CONSOLIDADO,
        "documentos_enviados": 500,
        "documentos_glosados": 50,
        "fonte": "demonstrativo consolidado",
    }
    primeira = api.put("/api/relatorios/baseline", json=corpo, headers=AUTH_HEADERS)
    assert primeira.status_code == 200
    assert primeira.json()["operadora_id"] is None

    segunda = api.put(
        "/api/relatorios/baseline",
        json={**corpo, "documentos_glosados": 12, "fonte": "demonstrativo revisado"},
        headers=AUTH_HEADERS,
    )

    assert segunda.status_code == 200
    assert segunda.json()["id"] == primeira.json()["id"]
    assert segunda.json()["documentos_glosados"] == 12
    assert segunda.json()["fonte"] == "demonstrativo revisado"
    # `updated_at` avança: o `onupdate` do model não dispara num INSERT ...
    # ON CONFLICT em Core, então ele é setado explicitamente no `set_`.
    assert segunda.json()["updated_at"] > primeira.json()["updated_at"]

    gravadas = sessao.execute(
        select(func.count())
        .select_from(BaselineCompetencia)
        .where(
            BaselineCompetencia.competencia == COMPETENCIA_CONSOLIDADO,
            BaselineCompetencia.operadora_id.is_(None),
        )
    ).scalar_one()
    assert gravadas == 1


def test_put_baseline_cria_e_o_segundo_put_atualiza_em_vez_de_duplicar(
    api: TestClient, sessao: Session, cenario: Cenario
) -> None:
    """Upsert pela chave natural: baseline é digitado à mão e é corrigido."""
    primeira = api.put(
        "/api/relatorios/baseline", json=_corpo_baseline(cenario), headers=AUTH_HEADERS
    )
    assert primeira.status_code == 200
    assert primeira.json()["documentos_glosados"] == 20

    segunda = api.put(
        "/api/relatorios/baseline",
        json=_corpo_baseline(cenario, documentos_glosados=7, fonte="planilha revisada"),
        headers=AUTH_HEADERS,
    )

    assert segunda.status_code == 200
    assert segunda.json()["id"] == primeira.json()["id"]
    assert segunda.json()["documentos_glosados"] == 7
    assert segunda.json()["fonte"] == "planilha revisada"

    gravadas = sessao.execute(
        select(func.count())
        .select_from(BaselineCompetencia)
        .where(BaselineCompetencia.operadora_id == cenario.operadora.id)
    ).scalar_one()
    assert gravadas == 1


def test_put_baseline_com_glosados_acima_de_enviados_responde_422(
    api: TestClient, cenario: Cenario
) -> None:
    resposta = api.put(
        "/api/relatorios/baseline",
        json=_corpo_baseline(cenario, documentos_enviados=10, documentos_glosados=11),
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 422


def test_put_baseline_com_operadora_inexistente_responde_422(api: TestClient) -> None:
    """A FK devolveria um `IntegrityError` cru; quem digitou o id errado precisa ler o motivo."""
    resposta = api.put(
        "/api/relatorios/baseline",
        json={
            "competencia": COMPETENCIA,
            "operadora_id": str(uuid.uuid4()),
            "documentos_enviados": 10,
            "documentos_glosados": 1,
            "fonte": "planilha",
        },
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 422
    assert resposta.json()["error"]["mensagem"] == "operadora não encontrada"


def test_get_baseline_lista_o_que_foi_registrado(api: TestClient, cenario: Cenario) -> None:
    api.put("/api/relatorios/baseline", json=_corpo_baseline(cenario), headers=AUTH_HEADERS)

    resposta = api.get("/api/relatorios/baseline", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    meus = [item for item in resposta.json() if item["operadora_id"] == str(cenario.operadora.id)]
    assert len(meus) == 1
    assert meus[0]["valor_glosado_centavos"] == 1_234_500


# --- métricas -----------------------------------------------------------------


def _metricas(api: TestClient, cenario: Cenario, extra: str = "") -> dict[str, object]:
    resposta = api.get(
        f"/api/relatorios/metricas?operadora_id={cenario.operadora.id}{extra}",
        headers=AUTH_HEADERS,
    )
    assert resposta.status_code == 200
    corpo: dict[str, object] = resposta.json()
    return corpo


def test_metricas_por_status_soma_o_total_e_taxa_bate(api: TestClient, cenario: Cenario) -> None:
    corpo = _metricas(
        api, cenario, extra=f"&competencia_inicio={COMPETENCIA}&competencia_fim={COMPETENCIA}"
    )

    (competencia,) = corpo["competencias"]  # type: ignore[misc]
    sistema = competencia["sistema"]
    assert competencia["competencia"] == COMPETENCIA
    assert sum(sistema["por_status"].values()) == sistema["documentos"] == 4
    assert sistema["por_status"]["incompleto"] == 1
    assert sistema["por_status"]["problema"] == 1
    assert sistema["por_status"]["aprovado"] == 1
    assert sistema["por_status"]["processando"] == 1
    # Toda chave do enum aparece, inclusive as zeradas.
    assert sistema["por_status"]["liberado"] == 0
    assert sistema["documentos_com_pendencia"] == 2
    assert sistema["taxa_documentos_com_pendencia"] == pytest.approx(0.5)
    assert sistema["pendencias_abertas"] == 3
    assert competencia["glosa_informada"] is None


def test_metricas_glosa_informada_aparece_depois_do_put_baseline(
    api: TestClient, cenario: Cenario
) -> None:
    api.put("/api/relatorios/baseline", json=_corpo_baseline(cenario), headers=AUTH_HEADERS)

    corpo = _metricas(api, cenario)

    por_competencia = {item["competencia"]: item for item in corpo["competencias"]}  # type: ignore[union-attr]
    glosa = por_competencia[COMPETENCIA]["glosa_informada"]
    assert glosa["documentos_enviados"] == 100
    assert glosa["taxa_glosa"] == pytest.approx(0.2)
    assert glosa["fonte"] == "demonstrativo da operadora"
    # A competência sem baseline continua `None`: ausência é informação.
    assert por_competencia[COMPETENCIA_RESOLUCAO]["glosa_informada"] is None


def test_metricas_tempo_medio_de_resolucao_em_horas(api: TestClient, cenario: Cenario) -> None:
    corpo = _metricas(
        api,
        cenario,
        extra=(
            f"&competencia_inicio={COMPETENCIA_RESOLUCAO}&competencia_fim={COMPETENCIA_RESOLUCAO}"
        ),
    )

    (competencia,) = corpo["competencias"]  # type: ignore[misc]
    assert competencia["sistema"]["tempo_medio_resolucao_horas"] == pytest.approx(5.0)


def test_metricas_tempo_medio_e_none_sem_pendencia_resolvida(
    api: TestClient, cenario: Cenario
) -> None:
    """`None` e não zero: zero significaria "resolvem instantaneamente"."""
    corpo = _metricas(
        api, cenario, extra=f"&competencia_inicio={COMPETENCIA}&competencia_fim={COMPETENCIA}"
    )

    (competencia,) = corpo["competencias"]  # type: ignore[misc]
    assert competencia["sistema"]["tempo_medio_resolucao_horas"] is None


def test_metricas_por_operadora_e_por_dia_cobrem_o_cenario(
    api: TestClient, cenario: Cenario
) -> None:
    corpo = _metricas(
        api, cenario, extra=f"&competencia_inicio={COMPETENCIA}&competencia_fim={COMPETENCIA}"
    )

    (operadora,) = corpo["por_operadora"]  # type: ignore[misc]
    assert operadora["operadora_id"] == str(cenario.operadora.id)
    assert operadora["nome"] == cenario.operadora.nome
    assert operadora["documentos"] == 4
    assert operadora["taxa_documentos_com_pendencia"] == pytest.approx(0.5)

    dias = {item["data"]: item["documentos"] for item in corpo["por_dia"]}  # type: ignore[union-attr]
    assert dias["2020-01-15"] == 1


def test_comparacao_glosa_e_none_com_um_baseline_so(api: TestClient, cenario: Cenario) -> None:
    """Nunca invente a outra ponta: sem duas competências não há comparação."""
    api.put("/api/relatorios/baseline", json=_corpo_baseline(cenario), headers=AUTH_HEADERS)

    assert _metricas(api, cenario)["comparacao_glosa"] is None


def test_comparacao_glosa_com_dois_baselines_mede_glosa_contra_glosa(
    api: TestClient, cenario: Cenario
) -> None:
    """Queda de glosa sai negativa em pontos percentuais — mesma medida nas duas pontas."""
    api.put("/api/relatorios/baseline", json=_corpo_baseline(cenario), headers=AUTH_HEADERS)
    api.put(
        "/api/relatorios/baseline",
        json=_corpo_baseline(
            cenario,
            competencia=COMPETENCIA_RESOLUCAO,
            documentos_enviados=100,
            documentos_glosados=5,
        ),
        headers=AUTH_HEADERS,
    )

    comparacao = _metricas(api, cenario)["comparacao_glosa"]

    assert comparacao is not None
    assert comparacao["competencia_inicial"] == COMPETENCIA
    assert comparacao["competencia_final"] == COMPETENCIA_RESOLUCAO
    assert comparacao["taxa_glosa_inicial"] == pytest.approx(0.2)
    assert comparacao["taxa_glosa_final"] == pytest.approx(0.05)
    assert comparacao["variacao_pontos_percentuais"] == pytest.approx(-15.0)
