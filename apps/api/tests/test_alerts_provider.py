"""Testes unitários do `UazapiProvider` e da factory — issue #9.

**Nenhuma requisição de rede.** Todo o tráfego é interceptado por
`httpx.MockTransport`, que é justamente por que o construtor do provider aceita
um `httpx.Client` pronto. O contrato exercitado aqui (método, path, header
`token` literal e minúsculo, corpo `{"number", "text"}`) é o verificado
empiricamente contra a instância real e documentado em `alerts/uazapi.py`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from homecareos.alerts.errors import EnvioError
from homecareos.alerts.provider import get_provider
from homecareos.alerts.uazapi import UazapiProvider
from homecareos.config import Settings

BASE_URL = "https://instancia-teste.uazapi.com"
# Valor reconhecível de propósito: os testes de vazamento procuram por ele.
TOKEN = "token-secreto-do-teste"
DESTINATARIO = "5521999999999"
MENSAGEM = "🚨 *Pendência crítica*\nPaciente: Maria de Souza"


def _provider(
    handler: httpx.MockTransport, base_url: str = BASE_URL, token: str = TOKEN
) -> UazapiProvider:
    return UazapiProvider(base_url=base_url, token=token, client=httpx.Client(transport=handler))


def test_envio_bem_sucedido_usa_o_contrato_verificado_da_uazapi() -> None:
    capturadas: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        capturadas.append(request)
        return httpx.Response(200, json={"id": "abc"})

    _provider(httpx.MockTransport(responder)).enviar(DESTINATARIO, MENSAGEM)

    (requisicao,) = capturadas
    assert requisicao.method == "POST"
    assert requisicao.url.path.endswith("/send/text")
    assert requisicao.headers["token"] == TOKEN
    assert json.loads(requisicao.content) == {"number": DESTINATARIO, "text": MENSAGEM}


def test_base_url_com_barra_no_fim_nao_produz_path_duplicado() -> None:
    capturadas: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        capturadas.append(request)
        return httpx.Response(200)

    _provider(httpx.MockTransport(responder), base_url=f"{BASE_URL}/").enviar(
        DESTINATARIO, MENSAGEM
    )

    (requisicao,) = capturadas
    assert requisicao.url.path == "/send/text"
    assert "//send/text" not in str(requisicao.url)


def test_token_invalido_vira_envio_error_com_status_e_corpo() -> None:
    """O corpo é o que diz se o token está errado ou o número é inválido."""

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "Invalid token.", "data": {}})

    with pytest.raises(EnvioError) as erro:
        _provider(httpx.MockTransport(responder)).enviar(DESTINATARIO, MENSAGEM)

    mensagem = str(erro.value)
    assert "401" in mensagem
    assert "Invalid token" in mensagem


def test_falha_de_transporte_vira_envio_error_encadeado() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("tempo esgotado", request=request)

    with pytest.raises(EnvioError) as erro:
        _provider(httpx.MockTransport(responder)).enviar(DESTINATARIO, MENSAGEM)

    assert isinstance(erro.value.__cause__, httpx.ConnectTimeout)
    assert "ConnectTimeout" in str(erro.value)


def test_token_nunca_aparece_em_repr_nem_em_mensagem_de_erro() -> None:
    """`str(EnvioError)` vai parar em `alertas_enviados.detalhe`; o token não pode ir junto."""

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "Invalid token."})

    provider = _provider(httpx.MockTransport(responder))

    assert TOKEN not in repr(provider)
    assert BASE_URL in repr(provider)

    with pytest.raises(EnvioError) as erro:
        provider.enviar(DESTINATARIO, MENSAGEM)

    assert TOKEN not in str(erro.value)


def test_falha_de_transporte_tambem_nao_vaza_o_token() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rota para o host", request=request)

    with pytest.raises(EnvioError) as erro:
        _provider(httpx.MockTransport(responder)).enviar(DESTINATARIO, MENSAGEM)

    assert TOKEN not in str(erro.value)


# --- factory ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "token"),
    [("", TOKEN), (BASE_URL, ""), ("", "")],
)
def test_get_provider_devolve_none_sem_gateway_configurado(base_url: str, token: str) -> None:
    """Sem gateway não há `NullProvider`: `None` é o que deixa o resumo dizer a verdade."""
    assert get_provider(Settings(uazapi_base_url=base_url, uazapi_token=token)) is None


def test_get_provider_devolve_o_provider_com_os_dois_preenchidos() -> None:
    provider = get_provider(Settings(uazapi_base_url=BASE_URL, uazapi_token=TOKEN))

    assert isinstance(provider, UazapiProvider)
    assert TOKEN not in repr(provider)
