"""Erros da trilha de alertas.

A separação entre as duas famílias é o que decide quem precisa agir:

- `AlertConfigError` é erro de quem **opera** o sistema (JSON malformado em
  `ALERTAS_DESTINATARIOS`, telefone impossível, tipo de alerta que não existe).
  Vira 422 no endpoint de varredura, com a mensagem dizendo o que consertar, e
  código de saída 1 no `python -m homecareos.alerts.scan`.
- `EnvioError` é falha do **gateway** (token recusado, número inválido, timeout).
  Não interrompe a varredura: vira uma linha `falha` em `alertas_enviados` e o
  próximo destinatário segue sendo notificado.
"""

from __future__ import annotations


class AlertError(Exception):
    """Base de qualquer falha da trilha de alertas."""


class AlertConfigError(AlertError):
    """A configuração de alertas está inválida e precisa de correção humana."""


class EnvioError(AlertError):
    """O gateway de WhatsApp não entregou a mensagem.

    A mensagem desta exceção é gravada em `alertas_enviados.detalhe` e por isso
    nunca pode conter o token da instância — ver `alerts/uazapi.py`.
    """
