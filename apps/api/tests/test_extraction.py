"""Testes do pipeline de extração (Trilha C). Nenhum teste toca a rede.

O seam é `homecareos.extraction.claude.anthropic_client`, substituído por
`monkeypatch` para devolver um `FakeAnthropic` (`tests/anthropic_fake.py`).
`RetryPolicy.sleep` é sempre injetado como no-op nos testes que passam por
retry, para a suíte não pagar os segundos reais de backoff.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from homecareos.config import Settings
from homecareos.extraction.budget import CostBudget
from homecareos.extraction.claude import ClaudeVisionProvider
from homecareos.extraction.errors import (
    BudgetExceededError,
    ExtractionIncompleteError,
    ExtractionRefusedError,
)
from homecareos.extraction.provider import NullExtractionProvider, get_provider
from homecareos.extraction.raw_store import InMemoryRawResponseStore
from homecareos.extraction.retry import RetryPolicy
from homecareos.extraction.schema import CategoriaProfissional, EvolucaoProntuario
from tests.anthropic_fake import FakeAnthropic

MODEL = "claude-opus-5-test"


@dataclass
class SimplePage:
    """A implementação mínima de `PaginaDocumento` usada pelos testes."""

    numero: int
    conteudo: bytes
    content_type: str


@pytest.fixture
def pagina() -> SimplePage:
    return SimplePage(
        numero=1,
        conteudo=b"\x89PNG\r\n\x1a\nfake-page-bytes",
        content_type="image/png",
    )


def _campos_completos() -> EvolucaoProntuario:
    return EvolucaoProntuario(
        nome_paciente="Maria da Silva",
        data_atendimento=date(2024, 3, 5),
        nome_profissional="João Souza",
        registro_coren="12.345",
        categoria_profissional=CategoriaProfissional.ENFERMEIRO,
        procedimentos_realizados=["curativo simples", "aferição de sinais vitais"],
        materiais_utilizados=["gaze", "soro fisiológico"],
        assinatura_profissional_presente=True,
        carimbo_presente=True,
        carimbo_legivel=True,
        assinatura_paciente_responsavel_presente=True,
        observacoes="paciente estável",
        campos_ilegiveis=[],
    )


def _no_retry_policy() -> RetryPolicy:
    """Retry real, mas sem esperar de verdade — a suíte não paga segundos de backoff."""
    return RetryPolicy(sleep=lambda _seconds: None, jitter=lambda: 0.0)


def _provider(
    fake: FakeAnthropic,
    *,
    monkeypatch: pytest.MonkeyPatch,
    raw_store: InMemoryRawResponseStore | None = None,
    budget: CostBudget | None = None,
    retry: RetryPolicy | None = None,
) -> ClaudeVisionProvider:
    monkeypatch.setattr("homecareos.extraction.claude.anthropic_client", lambda api_key: fake)
    return ClaudeVisionProvider(
        api_key="sk-test",
        model=MODEL,
        raw_store=raw_store if raw_store is not None else InMemoryRawResponseStore(),
        budget=budget,
        retry=retry if retry is not None else _no_retry_policy(),
    )


def _status_error(
    cls: type[anthropic.APIStatusError], status_code: int
) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=status_code, request=request)
    return cls(f"erro http {status_code}", response=response, body=None)


def _connection_error() -> anthropic.APIConnectionError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(request=request)


def _timeout_error() -> anthropic.APITimeoutError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APITimeoutError(request=request)


# --- AC1 + AC8: extração completa e raw_response persistido -------------------


def test_extracao_popula_todos_os_campos_e_monta_a_chamada_certa(
    pagina: SimplePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    campos = _campos_completos()
    fake = FakeAnthropic.answering(campos)
    store = InMemoryRawResponseStore()
    provider = _provider(fake, monkeypatch=monkeypatch, raw_store=store)

    resultado = provider.extract(pagina)

    assert resultado.campos == campos
    assert resultado.confianca == pytest.approx(1.0)
    assert resultado.provider == "anthropic"
    assert resultado.modelo == MODEL
    assert all(v == 1.0 for v in resultado.confianca_por_campo.values())

    # AC8: raw_response foi persistido e a chave devolvida está no resultado.
    assert resultado.raw_response_key is not None
    assert store.get(resultado.raw_response_key) == resultado.raw_response

    # A chamada foi montada certo: modelo, max_tokens, imagem antes do texto.
    request = fake.last_request()
    assert request["model"] == MODEL
    assert request["max_tokens"] == 8192
    assert request["output_format"] is EvolucaoProntuario
    blocks = fake.user_content_blocks()
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(pagina.conteudo).decode("ascii"),
    }
    assert blocks[1]["type"] == "text"


# --- AC2: campo ausente vira None + entra em campos_ilegiveis -----------------


def test_campo_ausente_fica_none_e_entra_em_campos_ilegiveis(
    pagina: SimplePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    campos = EvolucaoProntuario(
        nome_paciente=None,
        campos_ilegiveis=["nome_paciente", "registro_coren"],
    )
    fake = FakeAnthropic.answering(campos)
    provider = _provider(fake, monkeypatch=monkeypatch)

    resultado = provider.extract(pagina)

    assert resultado.campos.nome_paciente is None
    assert "nome_paciente" in resultado.campos.campos_ilegiveis
    assert "registro_coren" in resultado.campos.campos_ilegiveis
    assert resultado.confianca_por_campo["nome_paciente"] == 0.0
    assert resultado.confianca_por_campo["registro_coren"] == 0.0
    assert resultado.confianca < 1.0


# --- AC3: recusa vira ExtractionRefusedError, sem retry, sem IndexError -------


def test_recusa_vira_extraction_refused_error_sem_retry(
    pagina: SimplePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeAnthropic.refusing(category="general_harms")
    provider = _provider(fake, monkeypatch=monkeypatch)

    with pytest.raises(ExtractionRefusedError) as exc_info:
        provider.extract(pagina)

    assert exc_info.value.category == "general_harms"
    assert len(fake.requests) == 1


# --- AC4: max_tokens vira erro de extração incompleta, não sucesso ------------


def test_max_tokens_vira_erro_de_extracao_incompleta(
    pagina: SimplePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeAnthropic.truncated()
    provider = _provider(fake, monkeypatch=monkeypatch)

    with pytest.raises(ExtractionIncompleteError):
        provider.extract(pagina)

    assert len(fake.requests) == 1


# --- AC5: erros transitórios retentam e depois têm sucesso --------------------


@pytest.mark.parametrize(
    "erro_transitorio",
    [
        anthropic.RateLimitError.__name__,
        "timeout",
        "connection",
        "5xx",
    ],
)
def test_erro_transitorio_seguido_de_sucesso_retenta(
    erro_transitorio: str, pagina: SimplePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    campos = _campos_completos()
    erro: Exception
    if erro_transitorio == anthropic.RateLimitError.__name__:
        erro = _status_error(anthropic.RateLimitError, 429)
    elif erro_transitorio == "timeout":
        erro = _timeout_error()
    elif erro_transitorio == "connection":
        erro = _connection_error()
    else:
        erro = _status_error(anthropic.InternalServerError, 500)

    fake = FakeAnthropic.answering(campos, raises=erro, raises_times=1)
    provider = _provider(fake, monkeypatch=monkeypatch)

    resultado = provider.extract(pagina)

    assert resultado.campos == campos
    assert len(fake.requests) > 1


# --- AC6: BadRequestError falha na primeira tentativa, zero retries -----------


def test_bad_request_error_falha_sem_retry(
    pagina: SimplePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    erro = _status_error(anthropic.BadRequestError, 400)
    fake = FakeAnthropic(raises=erro, raises_times=999)
    provider = _provider(fake, monkeypatch=monkeypatch)

    with pytest.raises(anthropic.BadRequestError):
        provider.extract(pagina)

    assert len(fake.requests) == 1


# --- AC7: chave vazia devolve NullExtractionProvider, sem exceção -------------


def test_get_provider_sem_chave_devolve_null_provider(pagina: SimplePage) -> None:
    settings = Settings(anthropic_api_key="")

    provider = get_provider(settings)

    assert isinstance(provider, NullExtractionProvider)
    resultado = provider.extract(pagina)
    assert resultado.confianca == 0.0
    assert resultado.campos.nome_paciente is None
    assert "nome_paciente" in resultado.campos.campos_ilegiveis


# --- AC9: teto de orçamento nunca deixa a 3ª chamada chegar à API -------------


def test_orcamento_esgotado_barra_a_chamada_sem_retry(
    pagina: SimplePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    campos = _campos_completos()
    fake = FakeAnthropic.answering(campos)
    budget = CostBudget(max_usd=0.10, cost_per_call_usd=0.05)  # comporta exatamente 2 chamadas
    provider = _provider(fake, monkeypatch=monkeypatch, budget=budget)

    provider.extract(pagina)
    provider.extract(pagina)
    assert len(fake.requests) == 2

    with pytest.raises(BudgetExceededError):
        provider.extract(pagina)

    # A 3ª chamada nunca chegou à API, e o erro não foi retentado.
    assert len(fake.requests) == 2


def test_pagina_jpeg_vai_com_media_type_jpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Foto tirada em campo chega e permanece em JPEG; declarar `image/png`
    para bytes JPEG faz a API recusar a requisição."""
    fake = FakeAnthropic.answering(_campos_completos())
    provider = _provider(fake, monkeypatch=monkeypatch)
    pagina_jpeg = SimplePage(numero=1, conteudo=b"\xff\xd8\xff-fake", content_type="image/jpeg")

    provider.extract(pagina_jpeg)

    bloco = fake.requests[-1]["messages"][0]["content"][0]
    assert bloco["source"]["media_type"] == "image/jpeg"


def test_media_type_nao_suportado_falha_antes_de_chamar_a_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic.answering(_campos_completos())
    provider = _provider(fake, monkeypatch=monkeypatch)
    pagina_tiff = SimplePage(numero=1, conteudo=b"II*\x00", content_type="image/tiff")

    with pytest.raises(ExtractionIncompleteError):
        provider.extract(pagina_tiff)

    assert fake.requests == []


def test_json_truncado_pelo_sdk_vira_extracao_incompleta(
    pagina: SimplePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resposta cortada no meio pode falhar na validação do SDK antes de
    `stop_reason` ser inspecionado. Seja qual for a ordem, é extração
    incompleta — nunca sucesso silencioso."""
    fake = FakeAnthropic.failing(ValidationError.from_exception_data("EvolucaoProntuario", []))
    provider = _provider(fake, monkeypatch=monkeypatch)

    with pytest.raises(ExtractionIncompleteError):
        provider.extract(pagina)

    assert len(fake.requests) == 1  # não é transitório: não retenta


def test_confianca_tem_tres_niveis(pagina: SimplePage, monkeypatch: pytest.MonkeyPatch) -> None:
    """É na faixa do meio que mora a decisão de glosa: um COREN que *parece*
    dizer 12.345 não é um COREN nítido nem um carimbo borrado."""
    campos = _campos_completos()
    campos = campos.model_copy(
        update={"campos_ilegiveis": ["carimbo_legivel"], "campos_incertos": ["registro_coren"]}
    )
    provider = _provider(FakeAnthropic.answering(campos), monkeypatch=monkeypatch)

    resultado = provider.extract(pagina)

    assert resultado.confianca_por_campo["carimbo_legivel"] == 0.0
    assert resultado.confianca_por_campo["registro_coren"] == 0.5
    assert resultado.confianca_por_campo["nome_paciente"] == 1.0
