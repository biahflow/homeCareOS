"""Porta do provider de extração + implementação offline + factory.

Mesmo desenho de `portal_api.ai.responder` (Protocol + implementação real +
implementação offline + factory): `get_provider()` decide entre
`ClaudeVisionProvider` e `NullExtractionProvider` só a partir da config, sem o
chamador precisar saber qual chave está configurada.
"""

from __future__ import annotations

from typing import Protocol

from homecareos.config import Settings
from homecareos.extraction.budget import CostBudget
from homecareos.extraction.raw_store import InMemoryRawResponseStore, RawResponseStore
from homecareos.extraction.schema import EvolucaoProntuario, ExtractionResult, PaginaDocumento


class ExtractionProvider(Protocol):
    """Porta que qualquer provider de extração de campos implementa."""

    name: str

    def extract(self, pagina: PaginaDocumento, documento_id: str | None = None) -> ExtractionResult:
        """`documento_id` identifica o documento na chave do `RawResponseStore`.

        Opcional porque `PaginaDocumento` não carrega identidade de documento
        (ver `schema.py`); o pipeline de intake da Fase 2 passa o ID real.
        """
        ...


class NullExtractionProvider:
    """Usado quando `settings.anthropic_api_key` está vazia.

    Devolve um resultado vazio com `confianca=0.0` em vez de explodir — sem
    chave configurada, extração é indisponível por decisão de config, não uma
    falha de infraestrutura. Todo campo entra em `campos_ilegiveis`, porque de
    fato nenhum foi lido.
    """

    name = "null"

    def extract(self, pagina: PaginaDocumento, documento_id: str | None = None) -> ExtractionResult:
        campos = EvolucaoProntuario(
            campos_ilegiveis=[
                nome
                for nome in EvolucaoProntuario.model_fields
                if nome not in {"campos_ilegiveis", "campos_incertos"}
            ]
        )
        confianca_por_campo = dict.fromkeys(campos.campos_ilegiveis, 0.0)
        return ExtractionResult(
            campos=campos,
            confianca=0.0,
            confianca_por_campo=confianca_por_campo,
            raw_response={},
            modelo="",
            provider=self.name,
            raw_response_key=None,
        )


def get_provider(
    settings: Settings, raw_store: RawResponseStore | None = None
) -> ExtractionProvider:
    """Escolhe o provider a partir da config.

    Sem `anthropic_api_key`, devolve `NullExtractionProvider` — sem exceção,
    sem tentar falar com a rede. `raw_store` defaulta para
    `InMemoryRawResponseStore`; a Fase 2 passa `S3RawResponseStore`.
    """
    if not settings.anthropic_api_key:
        return NullExtractionProvider()

    from homecareos.extraction.claude import ClaudeVisionProvider

    budget = CostBudget(
        max_usd=settings.extraction_max_cost_usd_per_batch,
        cost_per_call_usd=settings.extraction_cost_per_call_usd,
    )
    return ClaudeVisionProvider(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        raw_store=raw_store if raw_store is not None else InMemoryRawResponseStore(),
        budget=budget,
    )
