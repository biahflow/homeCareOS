"""Agregações de métrica da conferência — e a honestidade da comparação antes/depois.

A issue #8 pede "evolução mês a mês (antes do sistema vs depois)". Existe uma
armadilha aí, e este módulo é desenhado para não cair nela:

- o que o **sistema mede** é *pendência detectada antes do envio* — problema que
  a conferência pegou a tempo;
- o que o **baseline** registra é *glosa* — o que a operadora recusou depois do
  envio.

**São medidas diferentes e não podem ser divididas uma pela outra.** Um número
único de "ROI" cruzando as duas estaria inventando uma relação que o dado não
sustenta. Por isso:

- cada competência expõe os dois blocos **lado a lado e nomeados** (`sistema` e
  `glosa_informada`), nunca fundidos;
- `comparacao_glosa` é **glosa contra glosa**: taxa da competência mais antiga
  com baseline registrado contra a mais recente com baseline registrado. Mesma
  medida nas duas pontas;
- `BaselineCompetencia` aceita qualquer competência, anterior ou posterior à
  implantação. É "dado de glosa informado manualmente", não "dado de antes".

## Definições que são decisão, não detalhe

- **`por_status` é foto do status atual.** Um documento que foi `problema`, foi
  corrigido e virou `liberado` aparece como `liberado`. Por isso ele sozinho
  **não serve** de indicador de qualidade: quanto melhor a correção funciona,
  melhor a foto fica, mesmo que o volume de problema tenha aumentado.
- **`documentos_com_pendencia` conta documento com ao menos uma pendência, em
  qualquer status** (`EXISTS` em `pendencias`, incluindo as já resolvidas). É a
  medida estável de "quantos exigiram intervenção", e é ela — não `por_status` —
  que serve para acompanhar mês a mês.
- **`tempo_medio_resolucao_horas`** é a média de `resolved_at - created_at` das
  pendências **resolvidas** cujo documento pertence à competência. `None` quando
  nenhuma foi resolvida ainda: zero seria "resolvem instantaneamente".
- **`pendencias_vencidas` / `pendencias_proximos_7_dias`** usam `datetime.now(UTC)`
  e a constante `JANELA_PROXIMA` de `api.routers.pendencias`, contando apenas
  pendências não resolvidas — exatamente a definição de
  `GET /api/pendencias/resumo`, para os dois painéis nunca discordarem.

Todas as agregações são uma consulta por bloco (competência, operadora, dia)
sobre uma subconsulta comum de documentos filtrados. Nenhuma consulta roda
dentro de laço.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import ColumnElement, Subquery, and_, exists, func, select
from sqlalchemy.orm import Session

from homecareos.api.routers.pendencias import JANELA_PROXIMA
from homecareos.db.models import (
    BaselineCompetencia,
    Documento,
    DocumentoStatus,
    Operadora,
    Pendencia,
    PendenciaStatus,
)
from homecareos.reports.schema import (
    ComparacaoGlosa,
    MetricasCompetencia,
    MetricasGlosaInformada,
    MetricasOperadora,
    MetricasResponse,
    MetricasSistema,
    VolumeDia,
)

# Quantas competências entram quando ninguém informa janela: um ano de história,
# que é o horizonte em que uma comparação mês a mês começa a dizer alguma coisa.
JANELA_PADRAO_COMPETENCIAS = 12

# Casas decimais das taxas (0..1) e das horas. Quatro casas na taxa porque com
# duas um lote pequeno de documentos arredondaria variações reais para zero.
CASAS_TAXA = 4
CASAS_HORAS = 2

NOME_SEM_OPERADORA = "(sem operadora)"


def calcular_metricas(
    session: Session,
    *,
    competencia_inicio: str | None,
    competencia_fim: str | None,
    operadora_id: uuid.UUID | None,
) -> MetricasResponse:
    """Monta a resposta de métricas para a janela de competências pedida."""
    competencias = _competencias_da_janela(
        session, inicio=competencia_inicio, fim=competencia_fim, operadora_id=operadora_id
    )
    if not competencias:
        return MetricasResponse(
            competencias=[], por_operadora=[], por_dia=[], comparacao_glosa=None
        )

    # Subconsulta comum: os documentos da janela, já com a marca de "exigiu
    # intervenção". Todos os blocos agregam sobre ela, o que garante que os três
    # painéis falem exatamente do mesmo conjunto de documentos.
    documentos = _documentos_da_janela(competencias, operadora_id)

    por_competencia = _montar_competencias(
        session,
        documentos,
        competencias,
        baselines=_baselines_da_janela(session, competencias, operadora_id),
    )

    return MetricasResponse(
        competencias=por_competencia,
        por_operadora=_montar_por_operadora(session, documentos),
        por_dia=_montar_por_dia(session, documentos),
        comparacao_glosa=_comparar_glosa(por_competencia),
    )


def _competencias_da_janela(
    session: Session, *, inicio: str | None, fim: str | None, operadora_id: uuid.UUID | None
) -> list[str]:
    """Competências que têm documento na janela, em ordem crescente.

    Comparar `YYYY-MM` como texto é comparar cronologicamente, porque o mês vem
    com zero à esquerda — não há necessidade de converter para data.

    Sem janela informada, as `JANELA_PADRAO_COMPETENCIAS` mais recentes: sem
    limite, a primeira competência cadastrada continuaria voltando na resposta
    para sempre, e o painel do mês ficaria dominado por história morta.
    """
    condicoes: list[ColumnElement[bool]] = []
    if operadora_id is not None:
        condicoes.append(Documento.operadora_id == operadora_id)
    if inicio is not None:
        condicoes.append(Documento.competencia >= inicio)
    if fim is not None:
        condicoes.append(Documento.competencia <= fim)

    stmt = select(Documento.competencia).where(*condicoes).group_by(Documento.competencia)
    if inicio is None and fim is None:
        stmt = stmt.order_by(Documento.competencia.desc()).limit(JANELA_PADRAO_COMPETENCIAS)

    return sorted(session.scalars(stmt))


def _documentos_da_janela(competencias: list[str], operadora_id: uuid.UUID | None) -> Subquery:
    """Subconsulta dos documentos da janela, com a marca `tem_pendencia` por linha.

    A marca é calculada aqui, e não dentro de cada agregação, porque um `EXISTS`
    dentro de um `FILTER` de agregado é frágil entre versões do Postgres — com a
    coluna já materializada, todo bloco agrega sobre um booleano simples.
    """
    tem_pendencia = (
        exists()
        .where(Pendencia.documento_id == Documento.id)
        .correlate(Documento)
        .label("tem_pendencia")
    )
    condicoes: list[ColumnElement[bool]] = [Documento.competencia.in_(competencias)]
    if operadora_id is not None:
        condicoes.append(Documento.operadora_id == operadora_id)
    return (
        select(
            Documento.id.label("documento_id"),
            Documento.competencia.label("competencia"),
            Documento.status.label("status"),
            Documento.operadora_id.label("operadora_id"),
            Documento.created_at.label("created_at"),
            tem_pendencia,
        )
        .where(*condicoes)
        .subquery()
    )


def _baselines_da_janela(
    session: Session, competencias: list[str], operadora_id: uuid.UUID | None
) -> dict[str, BaselineCompetencia]:
    """Baselines aplicáveis à janela, indexados por competência.

    Sem `operadora_id` na consulta, o baseline aplicável é o **consolidado**
    (`operadora_id IS NULL`); com operadora, é o dela. Somar os baselines por
    operadora para formar um consolidado seria inventar um total que ninguém
    informou — a operação pode ter registrado só algumas operadoras.
    """
    stmt = select(BaselineCompetencia).where(BaselineCompetencia.competencia.in_(competencias))
    if operadora_id is None:
        stmt = stmt.where(BaselineCompetencia.operadora_id.is_(None))
    else:
        stmt = stmt.where(BaselineCompetencia.operadora_id == operadora_id)
    return {baseline.competencia: baseline for baseline in session.scalars(stmt)}


def _taxa(numerador: int, denominador: int) -> float:
    """Razão 0..1, com zero no denominador devolvendo `0.0` em vez de explodir."""
    if denominador == 0:
        return 0.0
    return round(numerador / denominador, CASAS_TAXA)


def _montar_competencias(
    session: Session,
    documentos_da_janela: Subquery,
    competencias: list[str],
    *,
    baselines: dict[str, BaselineCompetencia],
) -> list[MetricasCompetencia]:
    """Bloco por competência: três consultas agregadas para a janela inteira.

    Nenhuma delas roda dentro de laço — uma consulta por competência
    transformaria um painel de doze meses em doze idas ao banco por bloco.
    """
    por_status: dict[str, dict[str, int]] = {
        competencia: {status.value: 0 for status in DocumentoStatus} for competencia in competencias
    }
    total: dict[str, int] = dict.fromkeys(competencias, 0)
    com_pendencia: dict[str, int] = dict.fromkeys(competencias, 0)
    # Uma linha por (competência, status): o total da competência é a soma das
    # linhas, e `documentos_com_pendencia` é a soma dos contadores filtrados —
    # sem risco de contar duas vezes, porque cada documento tem um status só.
    contagens = session.execute(
        select(
            documentos_da_janela.c.competencia,
            documentos_da_janela.c.status,
            func.count(),
            func.count().filter(documentos_da_janela.c.tem_pendencia),
        ).group_by(documentos_da_janela.c.competencia, documentos_da_janela.c.status)
    )
    for competencia, status, documentos, documentos_com_pendencia in contagens:
        por_status[competencia][status.value] = documentos
        total[competencia] += documentos
        com_pendencia[competencia] += documentos_com_pendencia

    agora = datetime.now(UTC)
    faixas: dict[str, tuple[int, int, int]] = {}
    linhas_pendencias = session.execute(
        select(
            documentos_da_janela.c.competencia,
            func.count(),
            func.count().filter(Pendencia.deadline < agora),
            func.count().filter(
                and_(Pendencia.deadline >= agora, Pendencia.deadline <= agora + JANELA_PROXIMA)
            ),
        )
        .select_from(Pendencia)
        .join(documentos_da_janela, documentos_da_janela.c.documento_id == Pendencia.documento_id)
        .where(Pendencia.status != PendenciaStatus.RESOLVIDA)
        .group_by(documentos_da_janela.c.competencia)
    )
    for competencia, abertas, vencidas, proximas in linhas_pendencias:
        faixas[competencia] = (abertas, vencidas, proximas)

    horas: dict[str, float] = {}
    linhas_resolucao = session.execute(
        select(
            documentos_da_janela.c.competencia,
            func.avg(func.extract("epoch", Pendencia.resolved_at - Pendencia.created_at)) / 3600.0,
        )
        .select_from(Pendencia)
        .join(documentos_da_janela, documentos_da_janela.c.documento_id == Pendencia.documento_id)
        .where(Pendencia.status == PendenciaStatus.RESOLVIDA)
        .group_by(documentos_da_janela.c.competencia)
    )
    for competencia, media_horas in linhas_resolucao:
        if media_horas is not None:
            horas[competencia] = float(media_horas)

    resultado: list[MetricasCompetencia] = []
    for competencia in competencias:
        abertas, vencidas, proximas = faixas.get(competencia, (0, 0, 0))
        media = horas.get(competencia)
        resultado.append(
            MetricasCompetencia(
                competencia=competencia,
                sistema=MetricasSistema(
                    documentos=total[competencia],
                    por_status=por_status[competencia],
                    documentos_com_pendencia=com_pendencia[competencia],
                    taxa_documentos_com_pendencia=_taxa(
                        com_pendencia[competencia], total[competencia]
                    ),
                    pendencias_abertas=abertas,
                    pendencias_vencidas=vencidas,
                    pendencias_proximos_7_dias=proximas,
                    tempo_medio_resolucao_horas=(
                        None if media is None else round(media, CASAS_HORAS)
                    ),
                ),
                glosa_informada=_glosa_informada(baselines.get(competencia)),
            )
        )
    return resultado


def _glosa_informada(baseline: BaselineCompetencia | None) -> MetricasGlosaInformada | None:
    if baseline is None:
        return None
    return MetricasGlosaInformada(
        documentos_enviados=baseline.documentos_enviados,
        documentos_glosados=baseline.documentos_glosados,
        taxa_glosa=_taxa(baseline.documentos_glosados, baseline.documentos_enviados),
        valor_glosado_centavos=baseline.valor_glosado_centavos,
        horas_conferencia=baseline.horas_conferencia,
        fonte=baseline.fonte,
    )


def _montar_por_operadora(session: Session, documentos: Subquery) -> list[MetricasOperadora]:
    """Uma linha por operadora, ordenada pela taxa de problema decrescente.

    Documentos sem operadora entram numa linha própria em vez de sumirem:
    justamente o documento que ninguém conseguiu associar é o que mais precisa
    aparecer.
    """
    linhas = session.execute(
        select(
            documentos.c.operadora_id,
            Operadora.nome,
            func.count(),
            func.count().filter(documentos.c.tem_pendencia),
        )
        .select_from(documentos)
        .outerjoin(Operadora, Operadora.id == documentos.c.operadora_id)
        .group_by(documentos.c.operadora_id, Operadora.nome)
    ).all()

    resultado = [
        MetricasOperadora(
            operadora_id=operadora_id,
            nome=nome if nome is not None else NOME_SEM_OPERADORA,
            documentos=total,
            documentos_com_pendencia=com_pendencia,
            taxa_documentos_com_pendencia=_taxa(com_pendencia, total),
        )
        for operadora_id, nome, total, com_pendencia in linhas
    ]
    resultado.sort(key=lambda item: item.taxa_documentos_com_pendencia, reverse=True)
    return resultado


def _montar_por_dia(session: Session, documentos: Subquery) -> list[VolumeDia]:
    """Documentos por dia, com a fronteira do dia fixada em UTC.

    `date_trunc` sobre `timestamptz` trunca no fuso da **sessão** do Postgres,
    não num fuso fixo: o mesmo dado produziria fronteiras de dia diferentes num
    servidor configurado fora de UTC, e um documento recebido às 22h viraria
    "ontem" ou "amanhã" conforme a configuração da instância.

    `timezone('UTC', ...)` converte para um timestamp **ingênuo já em UTC**
    antes de truncar. Sem isso, mesmo fixando o fuso no `date_trunc` (o terceiro
    argumento, do Postgres 16+), o valor voltaria como `timestamptz` e o
    `.date()` do Python poderia deslocá-lo de volta.
    """
    dia = func.date_trunc("day", func.timezone("UTC", documentos.c.created_at))
    linhas = session.execute(
        select(dia, func.count()).select_from(documentos).group_by(dia).order_by(dia)
    ).all()
    return [VolumeDia(data=_como_data(momento), documentos=total) for momento, total in linhas]


def _como_data(momento: datetime | date) -> date:
    return momento.date() if isinstance(momento, datetime) else momento


def _comparar_glosa(competencias: list[MetricasCompetencia]) -> ComparacaoGlosa | None:
    """Compara a glosa informada mais antiga com a mais recente da janela.

    `None` quando menos de duas competências têm baseline: a outra ponta não
    existe, e inventá-la (com zero, ou com a média das demais) transformaria o
    indicador principal da issue numa ficção.
    """
    com_baseline = [
        (competencia.competencia, competencia.glosa_informada.taxa_glosa)
        for competencia in competencias
        if competencia.glosa_informada is not None
    ]
    if len(com_baseline) < 2:
        return None

    competencia_inicial, taxa_inicial = com_baseline[0]
    competencia_final, taxa_final = com_baseline[-1]
    return ComparacaoGlosa(
        competencia_inicial=competencia_inicial,
        competencia_final=competencia_final,
        taxa_glosa_inicial=taxa_inicial,
        taxa_glosa_final=taxa_final,
        # Calculado a partir das taxas já arredondadas que saem na resposta, para
        # quem lê conseguir refazer a conta com os números que está vendo.
        variacao_pontos_percentuais=round((taxa_final - taxa_inicial) * 100, CASAS_HORAS),
    )
