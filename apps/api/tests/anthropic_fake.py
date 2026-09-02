"""Um Claude de mentira para `ClaudeVisionProvider` (ver `homecareos.extraction.claude`).

Mesma forma de `one/apps/api/tests/anthropic_fake.py`: o cliente real nunca é
tocado, `messages.parse(**kwargs)` grava o pedido verbatim (a prova do que
*não* foi enviado depende disso — ex.: que a imagem veio antes do texto, que o
modelo e o `max_tokens` são os combinados) e devolve exatamente o que o teste
mandar, por um construtor nomeado por caminho: `.answering(...)`,
`.refusing()`, `.truncated()`, `.failing(exc)`.

Este fake modela `client.messages.parse(...)`, não `client.messages.create(...)`
— por isso a resposta carrega `parsed_output` diretamente, no lugar de simular
o parsing interno do texto que o SDK real faz dentro de `post_parser`. O que
importa aqui é o contrato observável que `claude.py` consome:
`response.stop_reason`, `response.stop_details`, `response.parsed_output`,
`response.model`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homecareos.extraction.schema import EvolucaoProntuario


@dataclass
class FakeStopDetails:
    type: str = "refusal"
    category: str | None = None
    explanation: str | None = None


@dataclass
class FakeResponse:
    stop_reason: str = "end_turn"
    stop_details: FakeStopDetails | None = None
    model: str = "claude-opus-5"
    parsed_output: EvolucaoProntuario | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason,
            "stop_details": (
                None
                if self.stop_details is None
                else {
                    "type": self.stop_details.type,
                    "category": self.stop_details.category,
                    "explanation": self.stop_details.explanation,
                }
            ),
            "model": self.model,
            "parsed_output": (
                None if self.parsed_output is None else self.parsed_output.model_dump(mode="json")
            ),
        }


@dataclass
class FakeAnthropic:
    """O cliente. `.messages.parse(...)` grava o pedido e devolve o combinado."""

    #: Os campos já extraídos, prontos como o modelo os devolveria via
    #: `output_format`. `None` com `raises` significa "nem chegou lá".
    campos: EvolucaoProntuario | None = None
    stop_reason: str = "end_turn"
    stop_details: FakeStopDetails | None = None
    #: Provedor fora do ar: rede caída, 429, timeout, 5xx.
    raises: Exception | None = None
    #: Só a primeira chamada levanta `raises`; da segunda em diante, responde
    #: normalmente. Simula "RateLimitError seguido de sucesso" sem precisar de
    #: dois fakes coordenados por fora.
    raises_times: int = 0
    #: Tudo o que passou por `parse`, na ordem. A prova negativa mora aqui.
    requests: list[dict[str, Any]] = field(default_factory=list)

    # --- construtores, na voz do que o teste quer dizer ----------------------

    @classmethod
    def answering(cls, campos: EvolucaoProntuario, **kwargs: Any) -> FakeAnthropic:
        """Um modelo que respondeu no formato combinado."""
        return cls(campos=campos, **kwargs)

    @classmethod
    def truncated(cls, **kwargs: Any) -> FakeAnthropic:
        """O caso do teto de tokens: sem saída estruturada, `stop_reason="max_tokens"`."""
        return cls(campos=None, stop_reason="max_tokens", **kwargs)

    @classmethod
    def refusing(cls, category: str = "general_harms", **kwargs: Any) -> FakeAnthropic:
        """Classificador recusou: HTTP 200, sem saída estruturada, `stop_reason`."""
        return cls(
            campos=None,
            stop_reason="refusal",
            stop_details=FakeStopDetails(category=category),
            **kwargs,
        )

    @classmethod
    def failing(
        cls, exc: Exception | None = None, *, times: int = 1, **kwargs: Any
    ) -> FakeAnthropic:
        return cls(
            raises=exc or RuntimeError("provedor indisponível"),
            raises_times=times,
            **kwargs,
        )

    # --- a superfície que o provider usa -------------------------------------

    @property
    def messages(self) -> _FakeMessages:
        return _FakeMessages(self)

    def _parse(self, **kwargs: Any) -> FakeResponse:
        self.requests.append(kwargs)
        if self.raises is not None and len(self.requests) <= self.raises_times:
            raise self.raises
        return FakeResponse(
            stop_reason=self.stop_reason,
            stop_details=self.stop_details,
            model=str(kwargs.get("model", "claude-opus-5")),
            parsed_output=self.campos,
        )

    # --- o que o teste interroga ----------------------------------------------

    def last_request(self) -> dict[str, Any]:
        assert self.requests, "nenhum pedido chegou ao modelo"
        return self.requests[-1]

    def user_content_blocks(self) -> list[dict[str, Any]]:
        """Os blocos de conteúdo da mensagem de usuário do último pedido."""
        message = self.last_request()["messages"][-1]
        content = message["content"]
        assert isinstance(content, list), "conteúdo esperado como lista de blocos"
        return list(content)


@dataclass
class _FakeMessages:
    client: FakeAnthropic

    def parse(self, **kwargs: Any) -> FakeResponse:
        return self.client._parse(**kwargs)
