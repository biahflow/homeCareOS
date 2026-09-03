"""Modelo de dados da extração de campos de uma evolução de prontuário.

`EvolucaoProntuario` é o formato de saída estruturada que o provider de Vision
preenche (ver `claude.py`). Todos os campos são opcionais — ou, no caso de listas
e booleanos, defaultam para "nada encontrado" — porque o documento de origem é
uma foto de qualidade ruim e pode não trazer nada legível. `campos_ilegiveis`
carrega a explicação do que não pôde ser lido; é ela, não uma exceção, que o
fluxo de glosa consulta.

`PaginaDocumento` é o Protocol estrutural que substitui o import de
`homecareos.intake`: a trilha de intake está escrevendo `PageImage` ao mesmo
tempo que esta trilha escreve o extrator, e as duas não podem se acoplar antes
da Fase 2. Qualquer classe com `numero: int`, `conteudo: bytes` e
`content_type: str` satisfaz este Protocol estruturalmente — inclusive
`PageImage`, sem que este módulo a importe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


@runtime_checkable
class PaginaDocumento(Protocol):
    """O que este pacote consome de uma página de documento escaneado.

    Membros somente-leitura (`@property`) e não atributos anotados: este pacote
    só lê a página, nunca a escreve, e um Protocol com atributo anotado exige
    que a implementação seja *gravável* — o que exclui um dataclass congelado.
    `PageImage` (`intake/pdf.py`) é exatamente isso, e passou a satisfazer este
    Protocol quando a Fase 2 ligou as duas trilhas.
    """

    @property
    def numero(self) -> int: ...

    @property
    def conteudo(self) -> bytes:  # PNG ou JPEG
        ...

    @property
    def content_type(self) -> str: ...


class CategoriaProfissional(StrEnum):
    """Categorias profissionais que assinam uma evolução de home care."""

    ENFERMEIRO = "enfermeiro"
    TECNICO_ENFERMAGEM = "tecnico_enfermagem"
    FISIO = "fisio"
    FONO = "fono"
    MEDICO = "medico"


class EvolucaoProntuario(BaseModel):
    """Campos extraídos de uma evolução de prontuário de home care.

    Espelha exatamente os campos da issue #3. Nenhum campo é inventado: o que
    o modelo não conseguiu ler entra como `None` (ou lista vazia) e o nome do
    campo é registrado em `campos_ilegiveis` com o motivo — ver `prompt.py`.
    """

    nome_paciente: str | None = None
    data_atendimento: date | None = None
    nome_profissional: str | None = None
    registro_coren: str | None = None
    categoria_profissional: CategoriaProfissional | None = None
    procedimentos_realizados: list[str] = Field(default_factory=list)
    materiais_utilizados: list[str] = Field(default_factory=list)
    assinatura_profissional_presente: bool = False
    carimbo_presente: bool = False
    carimbo_legivel: bool = False
    assinatura_paciente_responsavel_presente: bool = False
    observacoes: str | None = None
    campos_ilegiveis: list[str] = Field(default_factory=list)
    campos_incertos: list[str] = Field(default_factory=list)
    """Campos que foram lidos, mas com dúvida — o meio-termo entre ler e não ler.

    Sem isso a confiança por campo seria binária, e é justamente na faixa do
    meio que mora a decisão de glosa: um COREN que *parece* dizer 12.345 não é
    a mesma coisa que um COREN nítido nem que um carimbo borrado.
    """


@dataclass(frozen=True)
class ExtractionResult:
    """Resultado do serviço de extração para uma página."""

    campos: EvolucaoProntuario
    confianca: float  # 0..1, agregado
    confianca_por_campo: dict[str, float]
    raw_response: dict[str, Any]  # payload cru do provider, para auditoria
    modelo: str
    provider: str
    raw_response_key: str | None = None  # chave devolvida por RawResponseStore.persist
