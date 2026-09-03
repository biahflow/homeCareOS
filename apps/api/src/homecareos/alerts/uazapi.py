"""Implementação da porta `WhatsAppProvider` sobre a uazapi.

Contrato verificado empiricamente contra a instância real em 2026-09-03:

```
POST {base_url}/send/text
headers: token: <token da instância>, Content-Type: application/json
body:    {"number": "5521999999999", "text": "..."}
```

- a base URL é **por instância** (`https://<subdominio>.uazapi.com`);
- sem header nenhum a API responde `401 {"code":401,"message":"Missing token."}`;
- com `token` errado, `401 {"code":401,"message":"Invalid token."}`;
- `Authorization: Bearer ...` e `apikey:` **não** são reconhecidos (respondem
  "Missing token."). O nome do header é `token`, literal e minúsculo — isso foi
  testado, não deduzido.

## O token nunca sai daqui

O token da instância é credencial de envio: quem o tem manda mensagem em nome
da empresa. Ele não pode aparecer em log, `repr`, mensagem de exceção nem em
linha de `alertas_enviados` — e a mensagem de `EnvioError` vai justamente para
`alertas_enviados.detalhe`. Por isso o `__repr__` abaixo é explícito (mostra a
base URL, omite o token) e o corpo da resposta é o único texto de terceiro que
entra no erro: ele diz *se o token está errado* sem dizer *qual* é.
"""

from __future__ import annotations

import httpx

from homecareos.alerts.errors import EnvioError

# Teto do corpo da resposta copiado para a mensagem de erro. O corpo útil da
# uazapi é um JSON de duas chaves; o que pode chegar grande é uma página de erro
# de proxy, e ela iria inteira para `alertas_enviados.detalhe`.
LIMITE_CORPO_NO_ERRO = 500


class UazapiProvider:
    """Envia texto pela API da instância uazapi configurada."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        """`client` existe para o teste injetar um `httpx.MockTransport`.

        Sem ele, testar o contrato (método, path, header `token`, corpo) exigiria
        ou uma requisição de rede real — impossível sem credencial, e indesejável
        com uma — ou um mock do módulo `httpx` inteiro, que provaria menos.
        """
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def __repr__(self) -> str:
        """Mostra a base URL e **omite o token** — ver a docstring do módulo."""
        return f"UazapiProvider(base_url={self._base_url!r}, token=<omitido>)"

    def enviar(self, destinatario: str, mensagem: str) -> None:
        """Entrega a mensagem pela instância. Levanta `EnvioError` em qualquer recusa."""
        try:
            resposta = self._client.post(
                f"{self._base_url}/send/text",
                headers={"token": self._token},
                json={"number": destinatario, "text": mensagem},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # Timeout, DNS, conexão recusada: falha de transporte, não recusa do
            # gateway. O tipo da exceção é o que distingue as duas para quem for
            # ler `alertas_enviados.detalhe` depois.
            raise EnvioError(
                f"falha de transporte ao falar com o gateway de WhatsApp: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not resposta.is_success:
            corpo = resposta.text[:LIMITE_CORPO_NO_ERRO]
            raise EnvioError(
                f"gateway de WhatsApp recusou o envio: HTTP {resposta.status_code} {corpo}"
            )
