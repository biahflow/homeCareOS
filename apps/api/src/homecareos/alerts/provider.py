"""Porta do gateway de WhatsApp + factory.

## DESVIO CONSCIENTE da issue #9: uazapi no lugar da Z-API

A issue #9 nomeia a Z-API. O gateway efetivamente contratado é a **uazapi**, e
é ela que esta trilha implementa. O desvio é barato justamente por causa desta
porta: `WhatsAppProvider` é o contrato mínimo que a camada de alertas conhece
("entregue este texto para este número, ou levante `EnvioError`"), e trocar de
gateway depois é escrever outra implementação e mudar configuração — não
reescrever detector, template, política anti-bombardeio nem log de auditoria.

Mesmo desenho de `extraction/provider.py` (Protocol + implementação real +
factory que decide só a partir da config), com uma diferença deliberada: aqui a
factory devolve `None` em vez de uma implementação nula. Extração sem provider
ainda precisa devolver um `ExtractionResult` para o pipeline seguir; alerta sem
gateway não tem nada para devolver, e um `NullProvider` que engolisse o envio
faria a varredura reportar sucesso sem nunca ter notificado ninguém. `None` é o
que permite ao `ResumoVarredura` dizer que o canal está indisponível.

Desde o ADR 0006 quem fala com esta porta é `alerts/canais.CanalWhatsApp`, e
não mais o serviço: o serviço conhece só `CanalAlerta`. Nada aqui mudou — o
contrato do gateway é o mesmo, e é justamente por ele ser mínimo que o segundo
canal coube sem tocar neste arquivo.
"""

from __future__ import annotations

from typing import Protocol

from homecareos.config import Settings


class WhatsAppProvider(Protocol):
    """Porta que qualquer gateway de WhatsApp implementa."""

    def enviar(self, destinatario: str, mensagem: str) -> None:
        """Entrega a mensagem. Levanta `EnvioError` quando o gateway recusa."""
        ...


def get_provider(settings: Settings) -> WhatsAppProvider | None:
    """`None` quando base URL ou token estão vazios — alertas desligados.

    Não é falha: rodar sem gateway configurado é um modo de operação legítimo
    (ambiente local, homologação sem instância). O sistema segue conferindo
    documento e abrindo pendência; só não notifica.
    """
    if not settings.uazapi_base_url or not settings.uazapi_token:
        return None

    from homecareos.alerts.uazapi import UazapiProvider

    return UazapiProvider(
        base_url=settings.uazapi_base_url,
        token=settings.uazapi_token,
        timeout=settings.alertas_timeout_segundos,
    )
