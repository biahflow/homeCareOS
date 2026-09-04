"""A conta do freio: contar consumos na janela, decidir o 429 e registrar o consumo.

Sem nada de HTTP aqui — a tradução para 429 vive em `limites/dependencies.py`,
como o `_bloqueado` do login vive em `auth/router.py` e não em
`auth/protecao.py`.

`agora` é sempre parâmetro nomeado, nunca `datetime.now()` lido lá dentro: é o
que permite ao teste envelhecer um consumo sem freezegun e sem dormir — o mesmo
contrato de `auth.protecao.avaliar_bloqueio`.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from homecareos.auth.schema import ROTULO_MAQUINA, Principal
from homecareos.db.models.consumo_rate_limit import ConsumoRateLimit
from homecareos.limites.schema import LimiteEstourado, LimitesDoRecurso, Recurso

JANELA = timedelta(hours=1)
"""Janela de contagem, igual para os quatro recursos.

Constante e não configuração, seguindo `auth.recuperacao.JANELA_DO_TETO` e
`alerts.service.JANELA_RATE_LIMIT`, que são as outras duas janelas de uma hora
do projeto. Ela é **janela de segurança ativa**: o expurgo por retenção que
apagar `consumos_rate_limit` dentro dela devolve cota a quem estourou o limite
— ver a seção "Retenção e expurgo de dados" do README.
"""

PREFIXO_USUARIO = "usuario:"
"""Namespace da chave de pessoa: `usuario:<uuid>`."""

CHAVE_MAQUINA = f"maquina:{ROTULO_MAQUINA}"
"""Chave única da integração máquina-a-máquina (`X-API-Key`).

Uma só para todas as chaves de API configuradas, e é consciente: `api_keys`
aceita várias justamente para permitir rotação sem downtime (ver `config.py`),
então distinguir os contadores por chave faria a rotação zerar o limite — e
guardar qual chave foi usada colocaria credencial na tabela.
"""


def chave_do_principal(principal: Principal) -> str:
    """A chave do contador: a identidade de quem está chamando, nunca o IP.

    Pessoa vira `usuario:<id>` — o **id**, nunca o e-mail: o contador não
    precisa de dado pessoal para contar, e o id é estável mesmo que a pessoa
    troque de e-mail. Máquina vira `maquina:api`, porque `usuario_id` é `None`
    para ela (não existe pessoa por trás da chave de integração, e forjar um id
    ali faria o contador apontar para alguém que não fez nada).

    Os prefixos tornam as duas famílias disjuntas por construção: nenhuma
    identidade de pessoa pode colidir com a de máquina numa coluna só.
    """
    if principal.tipo == "usuario" and principal.usuario_id is not None:
        return f"{PREFIXO_USUARIO}{principal.usuario_id}"
    return CHAVE_MAQUINA


def limite_do_principal(limites: LimitesDoRecurso, principal: Principal) -> int:
    """O limite que se aplica a quem está chamando: pessoa ou máquina."""
    return limites.maquina if principal.tipo == "maquina" else limites.pessoa


def contar_consumos(session: Session, *, chave: str, recurso: Recurso, desde: datetime) -> int:
    """Consumos daquela chave, naquele recurso, com `created_at >= desde`."""
    total = session.scalar(
        select(func.count())
        .select_from(ConsumoRateLimit)
        .where(
            ConsumoRateLimit.chave == chave,
            ConsumoRateLimit.recurso == recurso.value,
            ConsumoRateLimit.created_at >= desde,
        )
    )
    return total or 0


def _consumo_mais_antigo(
    session: Session, *, chave: str, recurso: Recurso, desde: datetime
) -> datetime | None:
    """`created_at` do consumo mais antigo ainda dentro da janela, ou `None`."""
    return session.scalar(
        select(func.min(ConsumoRateLimit.created_at)).where(
            ConsumoRateLimit.chave == chave,
            ConsumoRateLimit.recurso == recurso.value,
            ConsumoRateLimit.created_at >= desde,
        )
    )


def avaliar_limite(
    session: Session, *, chave: str, recurso: Recurso, limite: int, agora: datetime
) -> LimiteEstourado | None:
    """`None` quando a requisição pode prosseguir; `LimiteEstourado` quando não.

    A segunda query — a do `Retry-After` — só acontece no caminho de bloqueio,
    que é o caminho raro. A cota volta quando o consumo mais antigo da janela
    sai dela, e é esse instante que o header reporta: a janela menos a idade
    daquele consumo, arredondada para cima, com piso de 1 segundo (um
    `Retry-After: 0` convidaria o cliente a tentar de novo imediatamente).
    """
    inicio_janela = agora - JANELA
    consumos = contar_consumos(session, chave=chave, recurso=recurso, desde=inicio_janela)
    if consumos < limite:
        return None

    mais_antigo = _consumo_mais_antigo(session, chave=chave, recurso=recurso, desde=inicio_janela)
    if mais_antigo is None:
        # Só alcançável com limite configurado em zero (ou negativo): não há
        # consumo nenhum e mesmo assim a rota está fechada. A janela inteira é
        # a resposta honesta.
        segundos = int(JANELA.total_seconds())
    else:
        segundos = math.ceil((mais_antigo + JANELA - agora).total_seconds())
    return LimiteEstourado(recurso=recurso, limite=limite, segundos_restantes=max(1, segundos))


def registrar_consumo(session: Session, *, chave: str, recurso: Recurso) -> None:
    """Adiciona a linha do consumo à sessão. **Não commita** — quem chama decide.

    Quem chama é `limites.dependencies.limitar`, e ele commita na hora: as
    quatro rotas limitadas não têm commit próprio garantido (duas delas
    respondem `StreamingResponse`, cujo corpo é transmitido depois de a sessão
    já ter sido fechada), então deixar a linha pendurada na sessão significaria
    consumo que nunca chega ao banco — e um contador que não conta.
    """
    session.add(ConsumoRateLimit(chave=chave, recurso=recurso.value))
