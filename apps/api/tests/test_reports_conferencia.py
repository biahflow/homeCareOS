"""Testes unitários das regras puras do relatório de conferência — sem banco.

Severidade, ação necessária, leitura da data de atendimento e renderização do
CSV são decisão de produto e formato de arquivo: nenhuma delas precisa de
Postgres para ser provada, e todas quebram de forma silenciosa se ninguém
olhar (uma cor errada, um prazo faltando, uma coluna deslocada).
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime

import pytest

from homecareos.db.models.enums import DocumentoStatus, TipoDocumento
from homecareos.reports import csv_export
from homecareos.reports.conferencia import (
    acao_necessaria,
    data_atendimento_de,
    severidade_de,
)
from homecareos.reports.schema import LinhaConferencia, Severidade

# --- severidade ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "esperada"),
    [
        (DocumentoStatus.INCOMPLETO, Severidade.CRITICO),
        (DocumentoStatus.PROBLEMA, Severidade.ATENCAO),
        (DocumentoStatus.EM_CORRECAO, Severidade.ATENCAO),
        (DocumentoStatus.RESOLVIDO, Severidade.ATENCAO),
        (DocumentoStatus.APROVADO, Severidade.OK),
        (DocumentoStatus.LIBERADO, Severidade.OK),
        # `processando` é OK de propósito: ainda não há veredito sobre o
        # documento (ver `reports.conferencia._SEVERIDADE_POR_STATUS`).
        (DocumentoStatus.PROCESSANDO, Severidade.OK),
    ],
)
def test_severidade_de_cobre_todos_os_status(status: DocumentoStatus, esperada: Severidade) -> None:
    assert severidade_de(status) is esperada


def test_severidade_de_nao_esquece_nenhum_status() -> None:
    """Status novo em `DocumentoStatus` precisa ganhar severidade, não `KeyError` em produção."""
    for status in DocumentoStatus:
        assert severidade_de(status) in set(Severidade)


# --- ação necessária ----------------------------------------------------------

PRAZO = datetime(2026, 8, 14, 23, 59, 59, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "pendencias", "esperado"),
    [
        (
            DocumentoStatus.INCOMPLETO,
            2,
            "Documento volta para o campo: 2 pendência(s) a corrigir.",
        ),
        (DocumentoStatus.PROBLEMA, 3, "Conferir antes do envio: 3 pendência(s)."),
        (DocumentoStatus.EM_CORRECAO, 1, "Correção em andamento: 1 pendência(s)."),
        (DocumentoStatus.RESOLVIDO, 0, "Revalidar para liberar."),
        (DocumentoStatus.PROCESSANDO, 0, "Aguardando extração e classificação."),
        (DocumentoStatus.APROVADO, 0, "Nenhuma."),
        (DocumentoStatus.LIBERADO, 0, "Nenhuma."),
    ],
)
def test_acao_necessaria_sem_deadline(
    status: DocumentoStatus, pendencias: int, esperado: str
) -> None:
    assert acao_necessaria(status, pendencias, None) == esperado


@pytest.mark.parametrize(
    "status",
    [
        DocumentoStatus.INCOMPLETO,
        DocumentoStatus.PROBLEMA,
        DocumentoStatus.EM_CORRECAO,
        DocumentoStatus.RESOLVIDO,
        DocumentoStatus.PROCESSANDO,
    ],
)
def test_acao_necessaria_com_deadline_acrescenta_o_prazo(status: DocumentoStatus) -> None:
    texto = acao_necessaria(status, 1, PRAZO)

    assert texto == acao_necessaria(status, 1, None) + " Prazo: 14/08/2026."


@pytest.mark.parametrize("status", [DocumentoStatus.APROVADO, DocumentoStatus.LIBERADO])
def test_acao_necessaria_em_status_terminal_ignora_o_deadline(status: DocumentoStatus) -> None:
    """Anunciar prazo num documento já liberado é ruído que compete com o que precisa de ação."""
    assert acao_necessaria(status, 0, PRAZO) == "Nenhuma."


# --- data de atendimento vinda do JSONB da extração ---------------------------


def test_data_atendimento_de_le_o_caso_feliz() -> None:
    assert data_atendimento_de({"data_atendimento": "2026-08-14"}) == date(2026, 8, 14)


@pytest.mark.parametrize(
    "campos",
    [
        {},
        {"data_atendimento": None},
        # Formato brasileiro, número e lista: `campos_extraidos` é JSONB livre
        # preenchido por um provider de Vision, e nada disso pode levantar.
        {"data_atendimento": "14/08/2026"},
        {"data_atendimento": 123},
        {"data_atendimento": ["2026-08-14"]},
        {"data_atendimento": ""},
        None,
    ],
)
def test_data_atendimento_de_devolve_none_para_lixo(campos: dict[str, object] | None) -> None:
    assert data_atendimento_de(campos) is None


# --- CSV ----------------------------------------------------------------------


def _linha(**overrides: object) -> LinhaConferencia:
    base: dict[str, object] = {
        "documento_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "tipo": TipoDocumento.EVOLUCAO,
        "competencia": "2026-08",
        "status": DocumentoStatus.PROBLEMA,
        "severidade": Severidade.ATENCAO,
        "recebido_em": datetime(2026, 8, 14, 13, 45, tzinfo=UTC),
        "data_atendimento": date(2026, 8, 12),
        "paciente_id": None,
        "paciente_nome": "Maria de Souza",
        "operadora_id": None,
        "operadora_nome": "Unimed",
        "pendencias_abertas": 1,
        "problema_encontrado": "carimbo ilegível",
        "acao_necessaria": "Conferir antes do envio: 1 pendência(s).",
        "deadline": datetime(2026, 9, 10, 23, 59, tzinfo=UTC),
    }
    base.update(overrides)
    return LinhaConferencia.model_validate(base)


def _ler(texto: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(texto), delimiter=csv_export.DELIMITADOR))


def test_primeiro_pedaco_do_csv_traz_bom_e_cabecalho() -> None:
    """Sem BOM o Excel pt-BR lê o arquivo como ANSI e quebra toda a acentuação."""
    pedacos = list(csv_export.gerar_csv([[_linha()]]))

    assert pedacos[0].startswith(csv_export.BOM_UTF8)
    linhas = _ler(pedacos[0].removeprefix(csv_export.BOM_UTF8))
    assert linhas[0] == list(csv_export.CABECALHO)


def test_csv_sem_documento_ainda_sai_com_cabecalho() -> None:
    """Arquivo de zero byte pareceria falha de download, não filtro sem resultado."""
    pedacos = list(csv_export.gerar_csv([]))

    linhas = _ler("".join(pedacos).removeprefix(csv_export.BOM_UTF8))
    assert linhas == [list(csv_export.CABECALHO)]


def test_cabecalho_sai_uma_unica_vez_em_varias_paginas() -> None:
    pedacos = list(csv_export.gerar_csv([[_linha()], [_linha()]]))

    linhas = _ler("".join(pedacos).removeprefix(csv_export.BOM_UTF8))
    assert len(linhas) == 3
    assert linhas[0] == list(csv_export.CABECALHO)


def test_campo_com_delimitador_aspas_e_quebra_de_linha_volta_identico() -> None:
    """É este caso que justifica ter usado o módulo `csv` em vez de `";".join(...)`.

    `problema_encontrado` carrega `motivo_glosa`, texto livre digitado pela
    operação: um `;` no meio dele deslocaria silenciosamente todas as colunas
    seguintes da linha.
    """
    texto_hostil = 'falta assinatura; carimbo "ilegível"\nreenviar folha 2'

    saida = csv_export.render_csv([_linha(problema_encontrado=texto_hostil)], cabecalho=True)

    cabecalho, linha = _ler(saida)
    assert cabecalho == list(csv_export.CABECALHO)
    assert linha[cabecalho.index("problema_encontrado")] == texto_hostil


def test_valores_ausentes_viram_string_vazia() -> None:
    saida = csv_export.render_csv(
        [_linha(paciente_nome=None, operadora_nome=None, data_atendimento=None, deadline=None)],
        cabecalho=True,
    )

    cabecalho, linha = _ler(saida)
    for coluna in ("paciente", "operadora", "data_atendimento", "deadline"):
        assert linha[cabecalho.index(coluna)] == ""


def test_datas_saem_no_formato_brasileiro() -> None:
    saida = csv_export.render_csv([_linha()], cabecalho=True)

    cabecalho, linha = _ler(saida)
    assert linha[cabecalho.index("data_atendimento")] == "12/08/2026"
    assert linha[cabecalho.index("recebido_em")] == "14/08/2026 13:45"
    assert linha[cabecalho.index("deadline")] == "10/09/2026 23:59"


def test_nome_do_arquivo_usa_a_competencia_ou_todas() -> None:
    hoje = date(2026, 9, 3)

    assert csv_export.nome_arquivo("2026-08", hoje) == "conferencia-2026-08-20260903.csv"
    assert csv_export.nome_arquivo(None, hoje) == "conferencia-todas-20260903.csv"
