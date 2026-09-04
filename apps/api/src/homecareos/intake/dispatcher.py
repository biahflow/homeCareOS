"""Porta de disparo da extração, do lado de quem consome: o intake.

A porta é declarada aqui, e não no pacote `extraction`, de propósito. O intake
precisa dizer "extraia esta página" sem saber se isso acontece na mesma
requisição, numa fila ou num serviço separado — e sem importar nada do pacote
de extração. A implementação síncrona vive em
`homecareos.extraction.dispatcher`; trocá-la por uma que enfileira não exige
tocar em nada aqui.

O tipo da página é o `PageImage` do próprio intake. `PaginaDocumento`
(`extraction/schema.py`) é o Protocol estrutural equivalente do outro lado, e
`PageImage` o satisfaz sem que nenhum dos dois pacotes importe o outro.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from homecareos.intake.pdf import PageImage


class ExtractionDispatcher(Protocol):
    """Porta: entrega uma página já persistida para extração."""

    def dispatch(
        self,
        documento_id: uuid.UUID,
        pagina: PageImage,
        *,
        usuario: str = "sistema",
        usuario_id: uuid.UUID | None = None,
    ) -> None:
        """Dispara a extração da página do documento `documento_id`.

        `usuario`/`usuario_id` identificam quem originou o upload (issue #30)
        e chegam até `log_conferencia` na classificação automática — dados
        simples, não o `Principal` inteiro nem a sessão do request, porque o
        dispatcher abre sessão própria e pode um dia rodar em segundo plano,
        fora do ciclo de vida da requisição. O default `"sistema"`/`None` é o
        que mantém cron, script e chamada sem autor funcionando sem inventar
        pessoa nenhuma.

        Pode levantar exceção: o chamador (o serviço de intake) trata a falha
        sem desfazer o documento já commitado.
        """
        ...


class NullExtractionDispatcher:
    """Não dispara nada. Útil para desligar a extração sem mexer no intake."""

    def dispatch(
        self,
        documento_id: uuid.UUID,
        pagina: PageImage,
        *,
        usuario: str = "sistema",
        usuario_id: uuid.UUID | None = None,
    ) -> None:
        return None
