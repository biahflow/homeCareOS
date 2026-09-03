"""Contratos de entrada e saída dos relatórios e métricas (issue #8).

Os dois produtos desta trilha têm públicos diferentes e por isso schemas
diferentes: `LinhaConferencia` é a linha que a conferente lê todo dia (um
documento, o problema encontrado, a ação necessária), e `MetricasResponse` é a
visão de gestão. Nenhum dos dois mistura o que o sistema mediu com o que foi
informado à mão — ver `homecareos.reports.metricas`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from homecareos.db.models.enums import DocumentoStatus, TipoDocumento

# Competência é sempre `YYYY-MM` com mês entre 01 e 12. A mesma expressão vale
# para o query param e para o corpo do baseline: um "2026-13" gravado no
# baseline nunca casaria com competência nenhuma de `documentos` e produziria um
# baseline órfão silencioso.
PADRAO_COMPETENCIA = r"^\d{4}-(0[1-9]|1[0-2])$"


class Severidade(enum.StrEnum):
    """Gravidade da linha para o painel de conferência.

    Existe para o frontend não reimplementar a regra de cor ("aprovado verde,
    problema amarelo, incompleto vermelho") — o mapeamento status → severidade
    é decisão de produto e mora em `reports.conferencia.severidade_de`.
    """

    CRITICO = "CRITICO"
    ATENCAO = "ATENCAO"
    OK = "OK"


class LinhaConferencia(BaseModel):
    """Um documento da competência, com o problema encontrado e a ação necessária."""

    documento_id: uuid.UUID
    tipo: TipoDocumento
    competencia: str
    status: DocumentoStatus
    severidade: Severidade
    recebido_em: datetime
    # Vem da última extração do documento. `None` quando não há extração ainda
    # ou quando o campo veio ilegível/lixo — o relatório do dia não pode cair
    # por causa de uma extração ruim.
    data_atendimento: date | None
    paciente_id: uuid.UUID | None
    paciente_nome: str | None
    operadora_id: uuid.UUID | None
    operadora_nome: str | None
    pendencias_abertas: int
    # Descrições das pendências não resolvidas unidas por " | "; "" quando não
    # há nenhuma aberta.
    problema_encontrado: str
    acao_necessaria: str
    # Menor deadline entre as pendências não resolvidas do documento.
    deadline: datetime | None


class MetricasSistema(BaseModel):
    """O que a conferência mediu: pendência detectada **antes** do envio."""

    documentos: int
    por_status: dict[str, int]
    documentos_com_pendencia: int
    taxa_documentos_com_pendencia: float
    pendencias_abertas: int
    pendencias_vencidas: int
    pendencias_proximos_7_dias: int
    tempo_medio_resolucao_horas: float | None


class MetricasGlosaInformada(BaseModel):
    """O que foi informado à mão: glosa, ou seja, o que a operadora recusou **depois** do envio."""

    documentos_enviados: int
    documentos_glosados: int
    taxa_glosa: float
    valor_glosado_centavos: int | None
    horas_conferencia: float | None
    fonte: str


class MetricasCompetencia(BaseModel):
    """Os dois blocos de uma competência, lado a lado e nomeados — nunca fundidos."""

    competencia: str
    sistema: MetricasSistema
    # `None` quando não há baseline registrado para a competência: a ausência é
    # informação, e preenchê-la com zero mentiria "não houve glosa".
    glosa_informada: MetricasGlosaInformada | None


class MetricasOperadora(BaseModel):
    """Quanto trabalho cada operadora dá, na janela pedida."""

    # `None` agrupa os documentos sem operadora associada — exatamente os que
    # ninguém conseguiu vincular, que é o caso que mais interessa olhar.
    operadora_id: uuid.UUID | None
    nome: str
    documentos: int
    documentos_com_pendencia: int
    taxa_documentos_com_pendencia: float


class VolumeDia(BaseModel):
    """Documentos recebidos por dia, para enxergar o pico do fechamento."""

    data: date
    documentos: int


class ComparacaoGlosa(BaseModel):
    """Antes/depois honesto: mesma medida (glosa informada) nas duas pontas."""

    competencia_inicial: str
    competencia_final: str
    taxa_glosa_inicial: float
    taxa_glosa_final: float
    # `(final - inicial) * 100`. Queda de glosa é negativa.
    variacao_pontos_percentuais: float


class MetricasResponse(BaseModel):
    competencias: list[MetricasCompetencia]
    por_operadora: list[MetricasOperadora]
    por_dia: list[VolumeDia]
    # `None` enquanto menos de duas competências da janela tiverem baseline: não
    # há como comparar contra uma ponta que não existe.
    comparacao_glosa: ComparacaoGlosa | None


class BaselineUpsert(BaseModel):
    """Corpo do `PUT /api/relatorios/baseline` — dado digitado a partir de um demonstrativo."""

    competencia: str = Field(pattern=PADRAO_COMPETENCIA)
    # `None` = consolidado de todas as operadoras.
    operadora_id: uuid.UUID | None = None
    documentos_enviados: int = Field(ge=0)
    documentos_glosados: int = Field(ge=0)
    valor_glosado_centavos: int | None = Field(default=None, ge=0)
    horas_conferencia: float | None = Field(default=None, ge=0)
    fonte: str = Field(min_length=1)
    observacao: str | None = None

    @model_validator(mode="after")
    def _glosados_nao_passam_de_enviados(self) -> BaselineUpsert:
        """O `CheckConstraint` do banco é a rede de segurança, não a mensagem de erro.

        Deixar o banco recusar produziria um `IntegrityError` cru para quem
        digitou o número trocado; aqui a pessoa lê o que aconteceu.
        """
        if self.documentos_glosados > self.documentos_enviados:
            raise ValueError("documentos_glosados não pode ser maior que documentos_enviados")
        return self


class BaselineOut(BaseModel):
    id: uuid.UUID
    competencia: str
    operadora_id: uuid.UUID | None
    documentos_enviados: int
    documentos_glosados: int
    valor_glosado_centavos: int | None
    horas_conferencia: float | None
    fonte: str
    observacao: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
