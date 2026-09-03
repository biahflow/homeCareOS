"""Exportação do relatório de conferência em CSV que abre no Excel em português.

DESVIO CONSCIENTE da issue #8, que pede "CSV/Excel": entregamos **CSV**, não
`.xlsx`. Um `.xlsx` de verdade exigiria uma dependência nova (`openpyxl`) para
ganho nenhum neste momento — o arquivo abaixo abre com duplo clique no Excel,
com colunas separadas e acentuação correta. Fica registrado como decisão, não
como esquecimento.

Duas escolhas de formato não são estéticas, são o que faz o arquivo abrir certo:

- **Delimitador `;`**: o Excel em pt-BR usa a vírgula como separador decimal e
  por isso espera `;` como separador de coluna. Com `,` o arquivo inteiro abre
  numa coluna só.
- **BOM UTF-8 no início**: sem ele o Excel lê o arquivo como ANSI e toda
  acentuação de `problema_encontrado` e `acao_necessaria` sai quebrada.

E a escrita passa pelo módulo `csv` da biblioteca padrão, nunca por
`";".join(...)`: uma descrição de pendência com `;`, aspas ou quebra de linha
(e elas vêm de `motivo_glosa`, texto livre digitado pela operação) deslocaria
silenciosamente todas as colunas seguintes da linha.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime

from homecareos.reports.schema import LinhaConferencia

# Marca de ordem de bytes UTF-8. Ver a docstring do módulo.
BOM_UTF8 = "﻿"

DELIMITADOR = ";"

# Cabeçalho em português, na ordem em que `linha_csv` emite os campos.
CABECALHO = (
    "documento",
    "tipo",
    "competencia",
    "status",
    "severidade",
    "paciente",
    "operadora",
    "recebido_em",
    "data_atendimento",
    "pendencias_abertas",
    "problema_encontrado",
    "acao_necessaria",
    "deadline",
)


def formatar_data(valor: date | None) -> str:
    """`DD/MM/AAAA`, ou string vazia quando ausente."""
    if valor is None:
        return ""
    return f"{valor:%d/%m/%Y}"


def formatar_data_hora(valor: datetime | None) -> str:
    """`DD/MM/AAAA HH:MM` em UTC, ou string vazia quando ausente.

    Em UTC porque é o fuso em que o banco grava; converter para o fuso de quem
    baixa exigiria saber qual é, e um horário certo no fuso errado é pior que um
    horário declaradamente em UTC.
    """
    if valor is None:
        return ""
    return f"{valor.astimezone(UTC):%d/%m/%Y %H:%M}"


def linha_csv(linha: LinhaConferencia) -> list[str]:
    """Converte uma linha do relatório nos campos do CSV, na ordem de `CABECALHO`."""
    return [
        str(linha.documento_id),
        linha.tipo.value,
        linha.competencia,
        linha.status.value,
        linha.severidade.value,
        linha.paciente_nome or "",
        linha.operadora_nome or "",
        formatar_data_hora(linha.recebido_em),
        formatar_data(linha.data_atendimento),
        str(linha.pendencias_abertas),
        linha.problema_encontrado,
        linha.acao_necessaria,
        formatar_data_hora(linha.deadline),
    ]


def render_csv(linhas: Iterable[LinhaConferencia], *, cabecalho: bool) -> str:
    """Renderiza um bloco de linhas como texto CSV, com escape correto."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=DELIMITADOR)
    if cabecalho:
        writer.writerow(CABECALHO)
    for linha in linhas:
        writer.writerow(linha_csv(linha))
    return buffer.getvalue()


def gerar_csv(paginas: Iterable[list[LinhaConferencia]]) -> Iterator[str]:
    """Gera o CSV em pedaços, um por página do relatório.

    O BOM e o cabeçalho saem no primeiro pedaço. Quando o filtro não devolve
    documento nenhum, o arquivo ainda sai com BOM e cabeçalho: um CSV vazio de
    verdade (zero bytes) pareceria falha de download.
    """
    primeiro = True
    for pagina in paginas:
        texto = render_csv(pagina, cabecalho=primeiro)
        yield BOM_UTF8 + texto if primeiro else texto
        primeiro = False
    if primeiro:
        yield BOM_UTF8 + render_csv([], cabecalho=True)


def nome_arquivo(competencia: str | None, hoje: date) -> str:
    """`conferencia-<competência ou 'todas'>-<AAAAMMDD>.csv`."""
    return f"conferencia-{competencia or 'todas'}-{hoje:%Y%m%d}.csv"
