"""`ClaudeVisionProvider`: extração de campos via Claude Vision (SDK `anthropic`).

Toda a construção do cliente Anthropic mora dentro de `anthropic_client(...)` —
nenhum outro lugar deste pacote instancia um cliente. Isso mantém a porta aberta
para migrar para Amazon Bedrock trocando só o corpo desta função (por
`AnthropicBedrockMantle(aws_region=...)`, com o model ID recebendo o prefixo
`anthropic.`) sem tocar em `provider.py`, `retry.py`, `schema.py` ou
`prompt.py`. Bedrock não é implementado nesta rodada — só a porta é mantida
fechada corretamente.

`anthropic_client` é também o seam de teste: os testes substituem esta função
via `monkeypatch.setattr` por algo que devolve um `FakeAnthropic`
(`tests/anthropic_fake.py`), então nenhum teste toca a rede.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from homecareos.extraction.budget import CostBudget
from homecareos.extraction.errors import ExtractionIncompleteError, ExtractionRefusedError
from homecareos.extraction.prompt import SYSTEM_PROMPT
from homecareos.extraction.raw_store import RawResponseStore
from homecareos.extraction.retry import RetryPolicy
from homecareos.extraction.schema import EvolucaoProntuario, ExtractionResult, PaginaDocumento

MAX_TOKENS = 8192
"""Teto de thinking + resposta somados (o modelo faz thinking por padrão)."""

_USER_INSTRUCTION = "Extraia os campos da evolução de prontuário registrada nesta página."


def anthropic_client(api_key: str) -> Any:
    """O ponto de costura único de construção do cliente Anthropic.

    `max_retries=0` porque o retry é feito por `retry.py`, com prazo de parede
    — o retry embutido do SDK multiplicaria o tempo de bloqueio de um worker
    síncrono. `timeout=60.0` explícito pelo mesmo motivo: o teto de espera por
    tentativa precisa ser um número que este módulo controla, não o default do
    SDK.
    """
    import anthropic  # lazy: mantém o pacote opcional fora do caminho de extração

    return anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=60.0)


_MEDIA_TYPES_SUPORTADOS = frozenset({"image/png", "image/jpeg"})


def _image_content_block(pagina: PaginaDocumento) -> dict[str, Any]:
    """Monta o bloco de imagem com o media type real da página.

    Página de PDF vem renderizada em PNG, mas foto tirada em campo chega e
    permanece em JPEG — declarar `image/png` para bytes JPEG faz a API recusar
    a requisição.
    """
    if pagina.content_type not in _MEDIA_TYPES_SUPORTADOS:
        raise ExtractionIncompleteError(
            f"media type {pagina.content_type!r} não é suportado pelo provider de visão"
        )
    data = base64.b64encode(pagina.conteudo).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": pagina.content_type, "data": data},
    }


def _raw_payload(response: Any) -> dict[str, Any]:
    """Serializa a resposta do modelo para auditoria, real ou fake."""
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        return dict(dump())
    # Fallback defensivo: resposta sem `.model_dump()` (não deveria acontecer
    # com o SDK real nem com o fake deste pacote, mas evita quebrar por um
    # detalhe de serialização em vez de por um erro de extração de verdade).
    return {
        "stop_reason": getattr(response, "stop_reason", None),
        "model": getattr(response, "model", None),
    }


def _confianca(campos: EvolucaoProntuario) -> tuple[float, dict[str, float]]:
    """Confiança por campo: 0.0 para o que o próprio modelo listou como ilegível.

    Três níveis, a partir do que o próprio modelo declarou: 0.0 para campo em
    `campos_ilegiveis` (não leu), 0.5 para campo em `campos_incertos` (leu com
    dúvida) e 1.0 para o resto. A faixa do meio é a que interessa ao conferente
    — é o que vale conferir contra o documento físico. `confianca` agregada é a
    média simples.
    """
    ilegiveis = set(campos.campos_ilegiveis)
    incertos = set(campos.campos_incertos)

    def _score(nome: str) -> float:
        if nome in ilegiveis:
            return 0.0
        if nome in incertos:
            return 0.5
        return 1.0

    controle = {"campos_ilegiveis", "campos_incertos"}
    por_campo = {
        nome: _score(nome) for nome in EvolucaoProntuario.model_fields if nome not in controle
    }
    agregada = sum(por_campo.values()) / len(por_campo) if por_campo else 0.0
    return agregada, por_campo


@dataclass
class ClaudeVisionProvider:
    """Extrai `EvolucaoProntuario` de uma página escaneada via Claude Vision."""

    api_key: str
    model: str
    raw_store: RawResponseStore
    budget: CostBudget | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    name: str = "anthropic"
    _client: Any = field(default=None, init=False, repr=False)

    def _get_client(self) -> Any:
        """Constrói o cliente uma vez por provider e reaproveita.

        Antes ele era reconstruído a cada tentativa de retry, o que joga fora
        o pool de conexões justamente quando a API já está sob estresse.
        """
        if self._client is None:
            self._client = anthropic_client(self.api_key)
        return self._client

    def extract(self, pagina: PaginaDocumento, documento_id: str | None = None) -> ExtractionResult:
        """Extrai os campos de `pagina`.

        `documento_id` identifica o documento para a chave do `raw_store`
        (`extracoes/<documento_id>/...`); como `PaginaDocumento` não carrega
        identidade de documento (só de página — ver `schema.py`), o número da
        página é usado como identificador provisório quando o chamador não
        passa um. A Fase 2, que liga este provider ao pipeline de intake real,
        deve passar o ID do documento explicitamente.
        """
        if self.budget is not None:
            self.budget.reserve()

        doc_id = documento_id if documento_id is not None else str(pagina.numero)

        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        _image_content_block(pagina),
                        {"type": "text", "text": _USER_INSTRUCTION},
                    ],
                }
            ],
            "output_format": EvolucaoProntuario,
        }

        def _call() -> Any:
            try:
                response = self._get_client().messages.parse(**request_kwargs)
            except ValidationError as exc:
                # Resposta cortada no meio produz JSON inválido, e o SDK pode
                # falhar na validação antes de devolver o objeto — nesse caminho
                # `stop_reason` nunca chega a ser inspecionado. Seja qual for a
                # ordem, o desfecho é o mesmo: extração incompleta, não sucesso.
                raise ExtractionIncompleteError(
                    "resposta do modelo não validou contra o schema (provável truncamento)"
                ) from exc
            self._checar_stop_reason(response)
            return response

        response = self.retry.run(_call)

        campos = response.parsed_output
        if campos is None:
            # Não deveria acontecer com stop_reason normal e output_format
            # válido; defensivo contra uma resposta bem-formada sem conteúdo
            # estruturado (ex.: schema não seguido).
            raise ExtractionIncompleteError("resposta sem saída estruturada (parsed_output vazio)")

        confianca, confianca_por_campo = _confianca(campos)
        raw_response = _raw_payload(response)
        raw_response_key = self.raw_store.persist(doc_id, raw_response)

        return ExtractionResult(
            campos=campos,
            confianca=confianca,
            confianca_por_campo=confianca_por_campo,
            raw_response=raw_response,
            modelo=str(getattr(response, "model", self.model)),
            provider=self.name,
            raw_response_key=raw_response_key,
        )

    @staticmethod
    def _checar_stop_reason(response: Any) -> None:
        """Checa `stop_reason` **antes** de qualquer leitura de `content`/`parsed_output`.

        Recusa do classificador de segurança volta como HTTP 200 com
        `stop_reason == "refusal"`, não como exceção — código que pula direto
        para `content[0]` quebra exatamente aqui. `max_tokens` é tratado como
        extração incompleta, nunca como sucesso silencioso: `max_tokens=8192`
        cobre thinking + resposta somados, e um JSON cortado no meio não é um
        resultado utilizável.
        """
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise ExtractionRefusedError(category)
        if stop_reason == "max_tokens":
            raise ExtractionIncompleteError(
                "resposta cortada por max_tokens antes de terminar (thinking + resposta "
                f"excederam {MAX_TOKENS} tokens)"
            )
