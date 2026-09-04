"""`GET /api/documentos`, `GET /api/documentos/{id}`, `.../arquivo` e `.../revalidar`.

`POST /api/documentos` **não** mora aqui — continua em
`homecareos.intake.router`, que esta trilha não toca (é o contrato já
consumido pelo frontend).

`GET /api/documentos/{id}/arquivo` (issue #51) serve a página escaneada pela
própria API, em streaming, em vez de devolver uma URL assinada do storage. O
porquê está no ADR 0003 — em resumo: o presigned do MinIO aponta para a rede
interna do Compose, o do storage local devolve `file://`, e streaming mantém o
prontuário atrás da autorização que este router já aplica.

A revalidação é o único endpoint daqui que escreve: ela reaplica as regras
ativas sobre a última extração já existente e reclassifica o documento. Toda a
lógica vive em `homecareos.classification.service`; este módulo só traduz os
erros de domínio em status HTTP.

Desde a issue #30 a revalidação registra **quem** a pediu: `usuario` deixou de
ser o literal `"api"` e passa a vir do `Principal` da requisição. Com sessão de
usuário, `log_conferencia` guarda o e-mail e o `usuario_id` da pessoa; com
`X-API-Key`, continua `"api"` com `usuario_id` nulo — não há pessoa por trás da
chave, e forjar uma faria a auditoria mentir.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from homecareos.api.pagination import (
    PaginacaoParams,
    RespostaPaginada,
    envelope_paginado,
    paginacao_params,
)
from homecareos.auth.dependencies import exigir_papel, principal_atual
from homecareos.auth.schema import Papel, Principal
from homecareos.classification.errors import (
    DocumentoNaoEncontradoError,
    RevalidacaoIndisponivelError,
    TransicaoInvalidaError,
)
from homecareos.classification.service import revalidar_documento as revalidar
from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    Extracao,
    Pendencia,
    PendenciaStatus,
    ResultadoValidacao,
    TipoDocumento,
    Validacao,
)
from homecareos.db.session import get_session

# A montagem do storage vive em `intake.router` desde que ela existe, e é ela
# que os testes substituem. Importar de lá (em vez de declarar outra fábrica
# aqui) mantém **um** ponto de configuração e **um** ponto de override: dois
# provedores para o mesmo recurso significariam um teste trocando o storage do
# upload e não o da leitura.
from homecareos.intake.router import get_document_storage
from homecareos.limites.dependencies import limitar
from homecareos.limites.schema import Recurso
from homecareos.storage import DocumentStorage, ObjectNotFoundError, content_type_for_key

router = APIRouter(prefix="/api/documentos", tags=["documentos"])

_CARACTERES_PROIBIDOS_NO_NOME = re.compile(r"[^A-Za-z0-9._-]")


class DocumentoListItem(BaseModel):
    """Um documento na listagem — sem extração/validações (ver detalhe)."""

    id: uuid.UUID
    tipo: TipoDocumento
    competencia: str
    status: DocumentoStatus
    pagina: int | None
    paciente_id: uuid.UUID | None
    operadora_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ExtracaoResumo(BaseModel):
    id: uuid.UUID
    campos_extraidos: dict[str, Any]
    confianca: float
    confianca_por_campo: dict[str, Any]
    modelo: str
    provider: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ValidacaoResumo(BaseModel):
    id: uuid.UUID
    regra_id: uuid.UUID
    resultado: ResultadoValidacao
    detalhe: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentoDetalhe(DocumentoListItem):
    """Detalhe de um documento: os campos da listagem, mais extração e validações.

    `arquivo_url` mantém o nome apesar de ser uma **chave**, e isso é decisão
    consciente desta entrega (issue #51), não descuido: renomear um campo de
    resposta é quebra de contrato, e o contrato tipado (`packages/contracts`) e
    a tela que o consome (`apps/web`) estão fora do escopo desta tarefa. Um
    rename só na API deixaria o TypeScript declarando um campo que a API parou
    de mandar — quebra silenciosa, do tipo que só aparece como `undefined` na
    tela. O nome mentiroso é neutralizado aqui pela descrição do campo (que sai
    no OpenAPI) e, sobretudo, por `GET /api/documentos/{id}/arquivo`: quem quer
    ver o documento não precisa mais tocar nesta chave.
    """

    arquivo_url: str = Field(
        description=(
            "Chave do objeto no storage (`documentos/{id}/{sha256}.png`), **não** "
            "uma URL: não é acessível pelo navegador. Para ver o documento, use "
            "`GET /api/documentos/{id}/arquivo`."
        )
    )
    extracao: ExtracaoResumo | None
    validacoes: list[ValidacaoResumo]


@router.get(
    "",
    response_model=RespostaPaginada[DocumentoListItem],
    summary="Lista documentos em conferência",
    description="Filtra por competência, status, operadora e paciente. Paginado por offset.",
)
def listar_documentos(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PaginacaoParams, Depends(paginacao_params)],
    competencia: Annotated[str | None, Query(description="Competência `YYYY-MM`")] = None,
    status_filtro: Annotated[
        DocumentoStatus | None, Query(alias="status", description="Status do documento")
    ] = None,
    operadora_id: Annotated[uuid.UUID | None, Query()] = None,
    paciente_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RespostaPaginada[DocumentoListItem]:
    filtros = []
    if competencia is not None:
        filtros.append(Documento.competencia == competencia)
    if status_filtro is not None:
        filtros.append(Documento.status == status_filtro)
    if operadora_id is not None:
        filtros.append(Documento.operadora_id == operadora_id)
    if paciente_id is not None:
        filtros.append(Documento.paciente_id == paciente_id)

    total = session.execute(
        select(func.count()).select_from(Documento).where(*filtros)
    ).scalar_one()
    linhas = (
        session.execute(
            select(Documento)
            .where(*filtros)
            .order_by(Documento.created_at.desc())
            .limit(params.limite)
            .offset(params.offset)
        )
        .scalars()
        .all()
    )

    itens = [DocumentoListItem.model_validate(linha) for linha in linhas]
    return envelope_paginado(itens=itens, total=total, params=params)


@router.get(
    "/{documento_id}",
    response_model=DocumentoDetalhe,
    summary="Detalhe de um documento",
    description="Documento com a extração (quando concluída) e as validações já aplicadas.",
)
def obter_documento(
    documento_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> DocumentoDetalhe:
    documento = session.get(Documento, documento_id)
    if documento is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="documento não encontrado"
        )

    extracao = (
        session.execute(select(Extracao).where(Extracao.documento_id == documento_id))
        .scalars()
        .first()
    )
    validacoes = (
        session.execute(select(Validacao).where(Validacao.documento_id == documento_id))
        .scalars()
        .all()
    )

    return DocumentoDetalhe(
        **DocumentoListItem.model_validate(documento).model_dump(),
        arquivo_url=documento.arquivo_url,
        extracao=ExtracaoResumo.model_validate(extracao) if extracao is not None else None,
        validacoes=[ValidacaoResumo.model_validate(validacao) for validacao in validacoes],
    )


def _nome_para_download(documento: Documento) -> str:
    """Nome de arquivo legível para quem confere: `evolucao-2026-08-pagina-3.png`.

    Montado a partir do documento, e não da chave do storage: a chave é
    `documentos/{uuid}/{sha256}.png`, que não diz nada a ninguém e ainda
    exporia identificador interno num diálogo de "salvar como".

    O resultado é filtrado para `[A-Za-z0-9._-]`. `competencia` é texto que
    entrou pela API no upload, e nome de arquivo vai para dentro de um header:
    aspas e quebra de linha aqui seriam injeção de cabeçalho, não estética.
    """
    partes = [documento.tipo.value, documento.competencia]
    if documento.pagina is not None:
        partes.append(f"pagina-{documento.pagina}")
    extensao = PurePosixPath(documento.arquivo_url).suffix
    return _CARACTERES_PROIBIDOS_NO_NOME.sub("-", f"{'-'.join(partes)}{extensao}")


@router.get(
    "/{documento_id}/arquivo",
    response_class=StreamingResponse,
    summary="Serve o documento escaneado para conferência visual",
    description=(
        "Transmite o arquivo original da página pela própria API, com "
        "`Content-Disposition: inline` — quem confere compara o que a extração "
        "leu com o que está no papel, sem baixar nada. Responde 404 tanto para "
        "documento inexistente quanto para documento cujo arquivo não está mais "
        "no storage."
    ),
    responses={
        404: {"description": "Documento inexistente, ou arquivo ausente no storage"},
        429: {"description": "Limite de downloads por hora atingido para esta identidade"},
        503: {"description": "Storage de documentos indisponível"},
    },
    # Sem `dependencies=` de PAPEL própria: ler documento é dos três papéis, que
    # é exatamente a regra que o `include_router` de `main.py` já aplica a este
    # router. A exceção consciente daqui é a revalidação, mais abaixo.
    #
    # A dependency abaixo não é de papel: é o rate limit por identidade do ADR
    # 0005. Esta rota transmite o arquivo do storage e **ocupa um worker
    # enquanto transmite** (ADR 0003) — o limite existe para conter laço, não
    # uso, e por isso é o mais folgado dos quatro para pessoa: abrir documento é
    # o gesto mais frequente da conferência.
    dependencies=[Depends(limitar(Recurso.DOWNLOAD_ARQUIVO))],
)
def obter_arquivo_do_documento(
    documento_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    storage: Annotated[DocumentStorage, Depends(get_document_storage)],
) -> StreamingResponse:
    """Serve os bytes da página escaneada, em blocos, sem carregá-la na memória.

    Duas armadilhas moram neste handler, e as duas são de tempo:

    **A sessão do banco acaba antes do corpo.** `Depends(get_session)` fecha a
    sessão quando o handler retorna, e o corpo de um `StreamingResponse` só é
    transmitido depois disso — a mesma coisa que a docstring de
    `reports.router._stream_csv` documenta. Por isso tudo o que vem do
    `Documento` (chave, content type, nome) é lido **agora**, para locais, e o
    iterador entregue à resposta não toca no ORM.

    **O status é escolhido antes do primeiro byte.** Chave ausente no storage
    precisa virar 404, e não dá para trocar o status depois que a transmissão
    começou; é por isso que `storage.get` procura o objeto na chamada, e não na
    primeira iteração (ver o Protocol em `homecareos.storage`).

    Nada aqui é logado: a chave identifica o objeto de prontuário no bucket e o
    conteúdo é o prontuário.
    """
    documento = session.get(Documento, documento_id)
    if documento is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="documento não encontrado"
        )

    chave = documento.arquivo_url
    content_type = content_type_for_key(chave)
    nome = _nome_para_download(documento)

    try:
        blocos = storage.get(chave)
    except ObjectNotFoundError as exc:
        # 404, e não 500: o documento existe, o arquivo dele é que não está no
        # storage. Quem confere precisa ler "o arquivo não está lá" e abrir
        # chamado, não um erro genérico que parece defeito da aplicação.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="arquivo do documento não encontrado no storage",
        ) from exc
    # `StorageError` (storage fora do ar, sem permissão) sobe e vira 503 no
    # handler global de `api/errors.py` — é falha de infraestrutura, não
    # documento ausente, e os dois casos não podem responder a mesma coisa.

    return StreamingResponse(
        blocos,
        media_type=content_type,
        # `inline`, não `attachment`: a conferência é olhar o documento ao lado
        # do que a extração leu. `attachment` obrigaria a baixar e abrir fora
        # do sistema a cada documento conferido.
        headers={"Content-Disposition": f'inline; filename="{nome}"'},
    )


class RevalidacaoResponse(BaseModel):
    """Resultado de uma revalidação: onde o documento parou e quanto ainda falta."""

    documento_id: uuid.UUID
    status: DocumentoStatus
    pendencias_abertas: int


@router.post(
    "/{documento_id}/revalidar",
    response_model=RevalidacaoResponse,
    summary="Revalida um documento contra as regras ativas da operadora",
    description=(
        "Reaplica as regras ativas sobre a última extração já registrada e "
        "reclassifica o documento. Não chama o provider de extração de novo."
    ),
    # Autorização no ENDPOINT, e não no router — exceção consciente à regra
    # "auth por router" de `api/auth.py`. Este router mistura capacidades de
    # papéis diferentes: ler documento é dos três papéis, revalidar é ação de
    # conferência (conferente e coordenador). Aplicar a restrição mais estreita
    # no router fecharia a leitura para o gestor; aplicar a mais larga deixaria
    # a escrita aberta. A dependency do router continua valendo por baixo desta.
    dependencies=[Depends(exigir_papel(Papel.CONFERENTE, Papel.COORDENADOR))],
)
def revalidar_documento(
    documento_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(principal_atual)],
) -> RevalidacaoResponse:
    try:
        status_final = revalidar(
            session, documento_id, usuario=principal.rotulo, usuario_id=principal.usuario_id
        )
    except DocumentoNaoEncontradoError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (RevalidacaoIndisponivelError, TransicaoInvalidaError) as exc:
        # 409 e não 422: o corpo da requisição está correto — é o estado atual
        # do documento (sem operadora, sem extração, já terminal) que impede a
        # revalidação agora.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    pendencias_abertas = session.execute(
        select(func.count())
        .select_from(Pendencia)
        .where(
            Pendencia.documento_id == documento_id,
            Pendencia.status != PendenciaStatus.RESOLVIDA,
        )
    ).scalar_one()

    return RevalidacaoResponse(
        documento_id=documento_id,
        status=status_final,
        pendencias_abertas=pendencias_abertas,
    )
