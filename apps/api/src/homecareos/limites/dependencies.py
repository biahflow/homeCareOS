"""`limitar(recurso)` — a dependency que aplica o freio a uma rota cara.

Dependency parametrizada por recurso, e **não** middleware: o ADR 0005 foi
explícito em não aplicar limite uniforme sobre `/api/*`, e um middleware
forçaria uma chave e um limite únicos para rotas que não têm nada em comum
além do prefixo da URL. Aqui cada rota declara qual recurso ela consome, e o
limite de cada recurso é configurado separadamente.

Ordem dentro da requisição: as dependencies de `include_router` (a de papel)
rodam antes das do decorator da rota, então **um 403 por papel não consome
cota** — quem não pode entrar não gasta o limite de quem pode. O 401 de
credencial vem antes de tudo, por `principal_atual`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from homecareos.auth.dependencies import principal_atual
from homecareos.auth.schema import Principal
from homecareos.config import Settings, get_settings
from homecareos.db.session import get_session
from homecareos.limites import protecao
from homecareos.limites.schema import LimiteEstourado, Recurso, limites_do_recurso


def _limite_estourado(estouro: LimiteEstourado) -> HTTPException:
    """O 429 do rate limit. **Diz qual recurso foi limitado**, e é decisão do ADR.

    O 429 do login (`auth/router._bloqueado`) é genérico de propósito, para não
    virar oráculo de "esta conta existe". Aqui não há o que esconder: quem
    chegou até a rota já está autenticado como si mesmo, e omitir qual limite
    estourou só atrapalha quem precisa se corrigir.
    """
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"limite de {estouro.limite} requisições por hora para "
            f"{estouro.recurso.rotulo} atingido; tente de novo em "
            f"{estouro.segundos_restantes} segundos"
        ),
        headers={"Retry-After": str(estouro.segundos_restantes)},
    )


@cache
def limitar(recurso: Recurso) -> Callable[..., None]:
    """A dependency que conta, decide e registra o consumo de `recurso`.

    Uso: `dependencies=[Depends(limitar(Recurso.UPLOAD_DOCUMENTO))]` no
    decorator da rota.

    O `functools.cache` é o que faz duas chamadas com o mesmo recurso devolverem o
    **mesmo** objeto: sem ele, cada `limitar(...)` criaria uma função nova, e
    `app.dependency_overrides[limitar(Recurso.X)]` de um teste nunca casaria com
    a função que a rota registrou.

    **Registra o consumo ANTES de executar a rota, e isso é escolha consciente.**
    Uma requisição que depois falhe na validação (um upload com tipo de arquivo
    inválido, por exemplo) terá consumido cota sem ter custado a chamada de IA.
    É o lado conservador do erro: registrar só no sucesso deixaria um laço de
    requisições inválidas passar livre — e é justamente o laço que se quer
    conter.

    O `commit` é daqui, e não do handler: duas das quatro rotas respondem
    `StreamingResponse`, cujo corpo só é transmitido depois de a sessão da
    requisição ter sido fechada. Ele é seguro nesta posição porque nada mais
    escreveu na sessão ainda — as dependencies rodam antes do handler.
    """

    def dependency(
        principal: Annotated[Principal, Depends(principal_atual)],
        session: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> None:
        chave = protecao.chave_do_principal(principal)
        limite = protecao.limite_do_principal(limites_do_recurso(settings, recurso), principal)
        estouro = protecao.avaliar_limite(
            session,
            chave=chave,
            recurso=recurso,
            limite=limite,
            agora=datetime.now(UTC),
        )
        if estouro is not None:
            raise _limite_estourado(estouro)
        protecao.registrar_consumo(session, chave=chave, recurso=recurso)
        session.commit()

    return dependency
