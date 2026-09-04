"""`/api/alertas/canais` — o liga/desliga dos canais e o histórico dele (ADR 0006, parte 2).

Router **separado** de `alerts/router.py`, e a escolha segue o precedente de
`auth/auditoria_router.py`: aquele router é a varredura e o log de envios — o
que os alertas *fazem* —, e este é a configuração deles, com outro ciclo de
vida e outra autorização. Vive sob o mesmo prefixo (`/api/alertas/canais`)
porque semanticamente é de alerta que trata, mas como router e arquivo
próprios.

## Quem lê e quem escreve

`coordenador` e `gestor` **leem**; só o `coordenador` **escreve**. A regra mais
larga fica no `include_router(...)` de `main.py`, como em todo o resto de
`/api/*`, e o `PATCH` declara a sua própria — é a mesma exceção consciente à
regra "auth por router" que `POST /api/documentos/{id}/revalidar`,
`PATCH /api/pendencias/{id}` e os relatórios de gestão já fazem. Aplicar a
restrição estreita no router fecharia a leitura para o gestor; aplicar só a
larga deixaria a escrita aberta.

Ligar e desligar canal é **operação**, e quem opera é o coordenador (ADR 0006,
que manteve a matriz do ADR 0001 intacta: o gestor lê a operação inteira, não a
executa, e segue com um único write no sistema, o baseline). Ler é do gestor
porque "por que ninguém foi avisado?" é pergunta de quem acompanha a operação —
e o gestor já lê o log de `/api/alertas`, que expõe o e-mail de quem recebeu e
o texto do que foi dito.

`X-API-Key` passa por `exigir_papel` em qualquer papel, como em toda rota (ver
`auth/dependencies.exigir_papel`), e é por isso que o ator da auditoria tem o
id nullable.

## A resposta separa habilitado de disponível

    canal habilitado (banco)  x  credencial presente (.env)  =  canal envia

Os dois estados saem separados, canal a canal, porque é o que impede alguém de
ligar um canal na tela e não entender por que nada sai. `disponivel` é derivado
do `.env` e por isso **não** é editável por aqui: mudar credencial continua
sendo deploy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from homecareos.alerts import canais_repository
from homecareos.alerts.canais import montar_canais
from homecareos.alerts.schema import Canal
from homecareos.api.pagination import (
    PaginacaoParams,
    RespostaPaginada,
    envelope_paginado,
    paginacao_params,
)
from homecareos.auth.dependencies import exigir_papel, principal_atual
from homecareos.auth.schema import Papel, Principal
from homecareos.config import Settings, get_settings
from homecareos.db.session import get_session

router = APIRouter(prefix="/api/alertas/canais", tags=["alertas"])


class CanalOut(BaseModel):
    """O estado de um canal como a tela precisa vê-lo.

    Schema **próprio**, e não `alerts.schema.EstadoCanal`, que é o par de
    booleanos aninhado em `ResumoVarredura.canais`. Reusá-lo puro não serviria
    (falta quem decidiu e quando) e estendê-lo mudaria a saída de
    `POST /api/alertas/varredura` e do JSON do cron, que é contrato publicado e
    consumido — alargar um schema para servir a um segundo caso é como se
    ganha um campo que metade dos consumidores ignora. O par de booleanos
    continua vindo do mesmo objeto de domínio (`CanalAlerta.habilitado` e
    `.disponivel()`), então não há duas verdades sobre ele.
    """

    canal: Canal
    habilitado: bool
    """A decisão de quem opera, lida de `canais_alerta`."""

    disponivel: bool
    """Há credencial no `.env` para este canal enviar. Não é editável por aqui."""

    atualizado_em: datetime | None
    """Quando o estado atual foi decidido. `None` enquanto for o valor semeado
    pela migração de configuração — ninguém decidiu nada ainda, e um carimbo
    inventado faria a tela mentir sobre isso."""

    atualizado_por: str | None
    """Quem decidiu: o e-mail da pessoa, ou `"api"` para a chave de integração."""


class CanalAtualizarRequest(BaseModel):
    """Corpo de `PATCH /api/alertas/canais/{canal}`.

    Um campo só, e é o único que existe: credencial não se edita por API (ela
    vive no `.env`), e o nome do canal é o recurso, não um dado dele.
    """

    habilitado: bool


class AuditoriaCanalOut(BaseModel):
    """Um evento de `GET /api/alertas/canais/auditoria`.

    `usuario_id` é `None` e `usuario == "api"` quando o ator foi a chave de
    integração — ver `auth.schema.ROTULO_MAQUINA` e a docstring de
    `db/models/auditoria_canal.py`.
    """

    id: uuid.UUID
    usuario: str
    usuario_id: uuid.UUID | None
    canal: Canal
    habilitado_de: bool
    habilitado_para: bool
    created_at: datetime

    model_config = {"from_attributes": True}


def _estado_atual(session: Session, settings: Settings) -> list[CanalOut]:
    """Todos os canais do enum, sempre — mesmo os que não têm linha na tabela.

    Um canal que sumisse da resposta seria indistinguível de um canal que
    ninguém olhou, que é a mesma razão de `construir_canais` devolver todos.
    Sem linha, `habilitado` é `False`: é como a varredura o lê
    (`canais_repository.canais_habilitados`), e a tela não pode dizer outra
    coisa.
    """
    # `montar_canais` e não `construir_canais`: daqui só interessa `disponivel()`,
    # que sai do `.env` e não faz E/S. `habilitado` vem das linhas abaixo, na
    # mesma consulta que já precisa trazer `atualizado_em`/`atualizado_por` —
    # `construir_canais` faria uma segunda leitura da mesma tabela para
    # responder o que esta já respondeu. O `habilitados` vazio é ignorado.
    disponibilidade = {
        canal.canal: canal.disponivel()
        for canal in montar_canais(settings, habilitados=frozenset())
    }
    linhas = canais_repository.listar_estado(session)
    return [
        CanalOut(
            canal=canal,
            habilitado=linhas[canal].habilitado if canal in linhas else False,
            disponivel=disponibilidade.get(canal, False),
            atualizado_em=linhas[canal].atualizado_em if canal in linhas else None,
            atualizado_por=linhas[canal].atualizado_por if canal in linhas else None,
        )
        for canal in Canal
    ]


@router.get(
    "",
    response_model=list[CanalOut],
    summary="Estado de cada canal de alerta",
    description=(
        "Um item por canal, sempre todos. `habilitado` é a decisão de quem "
        "opera (banco); `disponivel` é a credencial no `.env`. Um canal só "
        "envia quando os dois são verdadeiros."
    ),
)
def listar_canais(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[CanalOut]:
    """Lista sem envelope de paginação, de propósito: o número de itens é o
    número de canais implementados, fechado por `alerts.schema.Canal`. Paginar
    um recurso de tamanho fixo seria cerimônia sem função, e obrigaria a tela a
    tratar uma segunda página que nunca existe.
    """
    return _estado_atual(session, settings)


@router.get(
    "/auditoria",
    response_model=RespostaPaginada[AuditoriaCanalOut],
    summary="Histórico de mudanças de canal",
    description=(
        "Quem ligou ou desligou qual canal, e quando. Paginado, do evento mais "
        "recente para o mais antigo. Filtra por `canal`, `ator_id` (quem agiu) "
        "e `habilitado` (o estado para o qual o canal foi movido — `false` "
        "responde 'quem silenciou a operação?')."
    ),
)
def listar_auditoria_de_canais(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PaginacaoParams, Depends(paginacao_params)],
    canal: Annotated[Canal | None, Query(description="Só eventos deste canal")] = None,
    ator_id: Annotated[
        uuid.UUID | None, Query(description="Só eventos feitos por este usuário")
    ] = None,
    habilitado: Annotated[
        bool | None, Query(description="Só eventos que moveram o canal para este estado")
    ] = None,
) -> RespostaPaginada[AuditoriaCanalOut]:
    linhas, total = canais_repository.listar_auditoria(
        session,
        canal=canal,
        ator_id=ator_id,
        habilitado=habilitado,
        limite=params.limite,
        offset=params.offset,
    )
    itens = [AuditoriaCanalOut.model_validate(linha) for linha in linhas]
    return envelope_paginado(itens=itens, total=total, params=params)


@router.patch(
    "/{canal}",
    response_model=CanalOut,
    summary="Liga ou desliga um canal de alerta",
    # Autorização no ENDPOINT, e não no router — exceção consciente à regra
    # "auth por router" de `api/auth.py`, pela mesma razão de
    # `POST /api/documentos/{id}/revalidar`: este router mistura capacidades de
    # papéis diferentes. Ler o estado dos canais é acompanhamento da operação
    # (coordenador e gestor); ligar e desligar é executá-la, e quem executa é o
    # coordenador (ADR 0006). A dependency do router continua valendo por baixo
    # desta.
    dependencies=[Depends(exigir_papel(Papel.COORDENADOR))],
    description=(
        "Só o coordenador. A mudança é auditada com ator, canal, de/para e "
        "quando; reenviar o valor que já está no banco não gera registro. Um "
        "canal ligado sem credencial no `.env` continua sem enviar, e a "
        "resposta diz isso em `disponivel`."
    ),
)
def atualizar_canal(
    canal: Canal,
    corpo: CanalAtualizarRequest,
    principal: Annotated[Principal, Depends(principal_atual)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CanalOut:
    """Escrita e auditoria num commit só, e nenhum dos dois sem o outro.

    `definir_habilitado` grava a linha de `canais_alerta` e a de
    `auditoria_canais_alerta` na mesma transação, e é ele quem decide se houve
    mudança de fato — ligar um canal já ligado não é evento. O `commit()` é
    daqui, único, pelo mesmo requisito duro da issue #30: se o registro
    saísse num commit separado, existiria a janela em que o canal já mudou e a
    auditoria ainda não sabe.

    Canal desconhecido nem chega aqui: o path param é `alerts.schema.Canal`, e
    o FastAPI responde 422 antes do handler.
    """
    canais_repository.definir_habilitado(
        session,
        canal=canal,
        habilitado=corpo.habilitado,
        ator=principal.rotulo,
        ator_id=principal.usuario_id,
        agora=datetime.now(UTC),
    )
    session.commit()

    atual = {estado.canal: estado for estado in _estado_atual(session, settings)}
    return atual[canal]
