"""`GET`, `POST` e `PATCH` de `/api/usuarios` — a administração de usuários (issue #30).

Quem administra usuário é o **coordenador**: decisão de produto tomada com o
cliente, que fecha a lacuna que o ADR 0001 deixou explicitamente em aberto
("a matriz de papéis acima é proposta"). O ADR 0004 registra a decisão.

Este é o endpoint mais perigoso da API — quem cria usuário decide quem entra —
e por isso cada regra abaixo é uma trava com razão nomeada, não estilo.

**1. Nem criar nem promover a `gestor`.** `PAPEIS_ATRIBUIVEIS` tem `conferente`
e `coordenador`, e só. `gestor` não é um degrau acima do coordenador: é outro
eixo da matriz (ADR 0001) — lê a operação inteira e é o único que escreve
baseline. Um coordenador que criasse um gestor estaria **se dando acesso a dado
de gestão que o papel dele não tem**: bastaria criar a conta e entrar nela.
Criar gestor continua sendo `python -m homecareos.auth.cli criar`, que exige
acesso ao servidor.

**2. A senha nunca passa pelo administrador.** A criação grava o hash de um
valor aleatório que é descartado na mesma linha — ninguém conhece essa senha,
nem quem criou a conta — e emite um token de recuperação, devolvido uma única
vez. A pessoa define a própria senha em `/redefinir-senha?token=…`. Uma senha
temporária escolhida por quem administra viraria um `Mudar@123` reusado na
operação inteira, e faria o administrador conhecer a credencial dos outros.

**3. Ninguém se tranca fora, e ninguém se promove.** Não se altera o próprio
papel — é o único papel cuja alteração interessa a quem ataca, e proibi-lo é o
que mantém a trava 1 valendo mesmo que a lista de papéis atribuíveis mude um dia.
Não se desativa a própria conta, e não se esvazia a coordenação (ver
`_recusar_esvaziar_a_coordenacao`).

**4. Desativar, nunca excluir.** `log_conferencia.usuario_id` aponta para
`usuarios`: apagar uma pessoa apagaria a resposta a "quem fez esta ação?", que é
a razão de existir da issue #30. Não há `DELETE` nesta rota, e não é omissão.
Desativar **revoga as sessões abertas** da pessoa — sem isso, quem foi desligado
às pressas continuaria navegando por até `SESSAO_DURACAO_HORAS` (12h) com o
cookie que já tem.

**5. Nada de credencial na resposta.** A saída é sempre `UsuarioOut`, uma
projeção explícita — nunca o model serializado. `senha_hash`, `mfa_secret` e
`mfa_ultimo_passo` não têm por onde escapar.

A autorização é aplicada no `include_router(...)` de `main.py`
(`exigir_papel(Papel.COORDENADOR)`), e não endpoint a endpoint: as três rotas
são do coordenador, e neste router — justamente neste — um endpoint novo precisa
nascer protegido por construção, sem depender de alguém lembrar de repetir a
dependency. A recusa do papel `gestor` continua sendo do endpoint, porque ela é
sobre o papel **atribuído**, não sobre quem chama.

Como em todo o resto de `/api/*`, `X-API-Key` passa por `exigir_papel` quando
`API_KEY_PAPEIS` declara `coordenador` (ADR 0007, ver
`auth/dependencies.exigir_papel`). As travas que dependem de "quem chama"
(item 3) não se aplicam à chave, que não tem "si mesmo"; as que dependem do
estado do sistema — o papel `gestor` e o último coordenador ativo — valem para
ela também, e é por isso que elas são verificadas contra o banco e não contra o
principal.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from homecareos.api.pagination import (
    PaginacaoParams,
    RespostaPaginada,
    envelope_paginado,
    paginacao_params,
)
from homecareos.auth import auditoria, recuperacao, senhas, sessoes
from homecareos.auth.dependencies import principal_atual
from homecareos.auth.router import normalizar_email
from homecareos.auth.schema import (
    AcaoAuditoriaUsuario,
    Papel,
    Principal,
    UsuarioAtualizarRequest,
    UsuarioCriadoOut,
    UsuarioCriarRequest,
    UsuarioOut,
)
from homecareos.config import Settings, get_settings
from homecareos.db.models import Usuario
from homecareos.db.session import get_session

router = APIRouter(prefix="/api/usuarios", tags=["usuarios"])

# Os papéis que esta API atribui. `gestor` fica de fora — ver o item 1 da
# docstring do módulo. A checagem é "está na lista?" e não "é gestor?" de
# propósito: um papel novo em `Papel` nasce **não** atribuível, e quem quiser
# torná-lo atribuível precisa vir aqui e decidir isso explicitamente.
PAPEIS_ATRIBUIVEIS = frozenset({Papel.CONFERENTE, Papel.COORDENADOR})

# Mensagem do 409 de e-mail repetido. Neutra de propósito: quem tem uma sessão
# de coordenador comprometida não pode usar a criação como oráculo para
# descobrir o nome e o papel de quem já está cadastrado. Ela não diz nada além
# do que quem chamou já sabe — o e-mail que ele mesmo digitou.
MENSAGEM_EMAIL_EM_USO = "e-mail já cadastrado"

MENSAGEM_USUARIO_NAO_ENCONTRADO = "usuário não encontrado"

# As três recusas do item 3. Mensagens **distintas**, ao contrário das do login:
# aqui não há nada a esconder de quem chama (é um coordenador autenticado agindo
# sobre o cadastro que ele mesmo administra), e uma mensagem genérica só faria a
# tela dizer "não deu" sem dizer o que fazer a respeito.
MENSAGEM_PROPRIO_PAPEL = "não é possível alterar o próprio papel; peça a outro coordenador"
MENSAGEM_AUTO_DESATIVACAO = "não é possível desativar a própria conta; peça a outro coordenador"
MENSAGEM_ULTIMO_COORDENADOR = (
    "não é possível desativar nem rebaixar o último coordenador ativo: a "
    "operação ficaria sem ninguém para administrar usuários e regras"
)

# Mensagem do 503 de token indisponível — ver `criar_usuario`.
MENSAGEM_TOKEN_INDISPONIVEL = (
    "não foi possível emitir o token de definição de senha; o usuário não foi criado"
)


def _recusar_papel_nao_atribuivel(papel: Papel) -> None:
    """403 para papel fora de `PAPEIS_ATRIBUIVEIS`. Vale na criação e na promoção.

    403 e não 422: o valor é um papel legítimo do sistema, o que falta é
    autorização para atribuí-lo por aqui. E a mensagem **diz o caminho certo**,
    ao contrário do 403 genérico de `auth/dependencies.MENSAGEM_SEM_PERMISSAO` —
    lá o silêncio protege (não ensinar qual papel comprometer), aqui quem chama
    já é coordenador e precisa saber que existe um caminho, senão vai procurar
    um jeito de contornar.
    """
    if papel in PAPEIS_ATRIBUIVEIS:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"o papel {papel.value!r} não é atribuível por esta API; ele é "
            "criado por linha de comando, no servidor "
            "(python -m homecareos.auth.cli criar)"
        ),
    )


def _coordenadores_ativos_alem_de(session: Session, usuario_id: uuid.UUID) -> int:
    """Quantos coordenadores ativos existem além deste usuário."""
    total = session.scalar(
        select(func.count())
        .select_from(Usuario)
        .where(
            Usuario.papel == Papel.COORDENADOR.value,
            Usuario.ativo.is_(True),
            Usuario.id != usuario_id,
        )
    )
    return int(total or 0)


def _recusar_esvaziar_a_coordenacao(
    session: Session, usuario: Usuario, corpo: UsuarioAtualizarRequest
) -> None:
    """409 quando a alteração tiraria o último coordenador ativo da operação.

    Sem coordenador ativo não sobra quem administre usuário nem quem edite
    regra, e a saída para destravar seria acesso ao banco ou ao servidor.

    Cobre os **dois** jeitos de esvaziar a coordenação, e não só o que a issue
    nomeia: desativar o último coordenador e rebaixá-lo a conferente têm
    exatamente a mesma consequência, e travar só o primeiro deixaria a porta
    aberta ao lado da que se fechou.

    Com sessão de usuário este caminho é, na prática, defesa em profundidade:
    quem chama é sempre um coordenador **ativo** (é o que `exigir_papel` deixa
    passar) e não pode agir sobre a própria conta, então sempre resta ele. Quem
    alcança esta recusa é a chave de máquina (`X-API-Key`) declarada como
    `coordenador`, que passa por `exigir_papel` e não tem "si mesmo" — para ela,
    esta é a única trava. É por isso que a verificação é contra o banco, e não
    contra o principal.
    """
    if usuario.papel != Papel.COORDENADOR.value or not usuario.ativo:
        return
    perde_o_papel = corpo.papel is not None and corpo.papel != Papel.COORDENADOR
    sai_de_atividade = corpo.ativo is False
    if not (perde_o_papel or sai_de_atividade):
        return
    if _coordenadores_ativos_alem_de(session, usuario.id) == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MENSAGEM_ULTIMO_COORDENADOR,
        )


@router.get(
    "",
    response_model=RespostaPaginada[UsuarioOut],
    summary="Lista os usuários da operação",
    description=(
        "Filtra por `ativo` (`true` só ativos, `false` só desativados, ausente "
        "todos). Paginado por offset. Nunca devolve `senha_hash`, `mfa_secret` "
        "nem `mfa_ultimo_passo`."
    ),
)
def listar_usuarios(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PaginacaoParams, Depends(paginacao_params)],
    ativo: Annotated[
        bool | None, Query(description="Só ativos (`true`) ou só desativados (`false`)")
    ] = None,
) -> RespostaPaginada[UsuarioOut]:
    """A listagem mostra e-mail, e aqui isso é o esperado.

    O e-mail é o identificador de login e quem lê esta rota é o coordenador, que
    administra exatamente essas contas — esconder o e-mail dele tornaria a tela
    inútil sem proteger ninguém. O cuidado com enumeração de usuário mora nas
    rotas **públicas** (login, `/senha/esqueci`), onde quem pergunta não provou
    ser ninguém.
    """
    stmt = select(Usuario)
    contagem_stmt = select(func.count()).select_from(Usuario)
    if ativo is not None:
        stmt = stmt.where(Usuario.ativo.is_(ativo))
        contagem_stmt = contagem_stmt.where(Usuario.ativo.is_(ativo))

    total = session.execute(contagem_stmt).scalar_one()
    linhas = (
        session.execute(
            # Desempate por e-mail (que é único): sem ele, duas pessoas de mesmo
            # nome têm ordem indefinida entre páginas, e uma delas pode aparecer
            # duas vezes ou nenhuma na paginação por offset.
            stmt.order_by(Usuario.nome, Usuario.email).limit(params.limite).offset(params.offset)
        )
        .scalars()
        .all()
    )

    itens = [UsuarioOut.model_validate(linha) for linha in linhas]
    return envelope_paginado(itens=itens, total=total, params=params)


@router.post(
    "",
    response_model=UsuarioCriadoOut,
    status_code=status.HTTP_201_CREATED,
    summary="Cria um usuário e emite o token de definição de senha",
    description=(
        "Cria a conta **sem senha conhecida por ninguém** e devolve, uma única "
        "vez, o token com que a pessoa define a própria senha em "
        "`{FRONTEND_BASE_URL}/redefinir-senha?token=<token>`. O token vale "
        "`SENHA_RESET_VALIDADE_MINUTOS` e é de uso único; depois disso o "
        "caminho é a pessoa pedir outro link em `POST /api/auth/senha/esqueci`. "
        "Papel `gestor`: 403. E-mail já cadastrado: 409."
    ),
)
def criar_usuario(
    corpo: UsuarioCriarRequest,
    principal: Annotated[Principal, Depends(principal_atual)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UsuarioCriadoOut:
    """A conta, o registro de auditoria e o token entram no **mesmo commit**, ou nenhum dos três.

    O `flush()` (e não um `SELECT` prévio "já existe esse e-mail?") é o que
    decide a colisão de cadastro: entre a consulta e o `INSERT` cabe outro
    cadastro, e o índice único de `usuarios.email` é a única autoridade sobre
    isso — ver a docstring de `db/models/usuario.py`.

    O registro de auditoria (issue #30) é enfileirado **antes** de
    `emitir_token`, de propósito: `criar_usuario` não tem `session.commit()`
    próprio, e quem commita é `recuperacao.emitir_token` — enfileirar depois
    da chamada deixaria o registro fora daquele commit. `usuario.id` já existe
    aqui (é `default=uuid.uuid4` client-side, `db/models/usuario.py:41`), então
    não é preciso esperar o `flush()` para ter o alvo.

    `emitir_token` devolve `None` quando o teto de emissões por hora do usuário
    foi atingido. Para uma conta recém-criada isso só acontece com
    `SENHA_RESET_MAX_POR_HORA <= 0` (configuração), mas **não pode passar em
    silêncio**: sem o token, a conta nasceria sem nenhum caminho de primeiro
    acesso e ninguém perceberia até a pessoa reclamar. O `None` não commitou
    nada, então o `rollback()` desfaz também a criação **e o registro de
    auditoria** — e o 503 diz que o usuário não foi criado, o que é verdade e é
    o que permite tentar de novo.
    """
    agora = datetime.now(UTC)
    _recusar_papel_nao_atribuivel(corpo.papel)

    usuario = Usuario(
        nome=corpo.nome,
        email=normalizar_email(corpo.email),
        # Senha morta: aleatória, hasheada e descartada na mesma expressão —
        # ninguém a conhece, nem quem está criando a conta. `senha_hash` é
        # `NOT NULL` e torná-la anulável seria migration; e mesmo com a coluna
        # anulável esta linha continuaria sendo a escolha certa, porque uma
        # senha ausente é um estado a mais para todo caminho de login tratar,
        # enquanto uma senha que ninguém conhece não abre nada por construção.
        senha_hash=senhas.gerar_hash(secrets.token_urlsafe(32)),
        papel=corpo.papel.value,
    )
    session.add(usuario)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MENSAGEM_EMAIL_EM_USO,
        ) from exc

    auditoria.registrar(
        session,
        usuario=principal.rotulo,
        usuario_id=principal.usuario_id,
        alvo_usuario_id=usuario.id,
        alvo_email=usuario.email,
        acao=AcaoAuditoriaUsuario.CRIACAO,
        mudancas={
            "nome": {"de": None, "para": usuario.nome},
            "papel": {"de": None, "para": usuario.papel},
        },
    )

    token = recuperacao.emitir_token(session, usuario, settings=settings, agora=agora)
    if token is None:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MENSAGEM_TOKEN_INDISPONIVEL,
        )

    return UsuarioCriadoOut(
        usuario=UsuarioOut.model_validate(usuario),
        token_definicao_senha=token,
    )


@router.patch(
    "/{usuario_id}",
    response_model=UsuarioOut,
    summary="Altera nome, papel e situação de um usuário",
    description=(
        "Altera nome, papel e `ativo`; qualquer campo omitido fica como está. "
        "Desativar **revoga todas as sessões abertas** da pessoa. Promover a "
        "`gestor`: 403. Alterar o próprio papel ou desativar a própria conta: "
        "403. Desativar ou rebaixar o último coordenador ativo: 409. Não existe "
        "`DELETE`: a auditoria referencia o usuário."
    ),
)
def atualizar_usuario(
    usuario_id: uuid.UUID,
    corpo: UsuarioAtualizarRequest,
    principal: Annotated[Principal, Depends(principal_atual)],
    session: Annotated[Session, Depends(get_session)],
) -> UsuarioOut:
    """As travas primeiro, a escrita depois — nenhuma alteração parcial sobrevive a uma recusa.

    A ordem das recusas é o desenho: as duas que dependem de **quem chama** vêm
    antes da que depende do **estado do sistema**, porque para quem clicou em
    "desativar" na própria linha "não é possível desativar a própria conta" é a
    resposta útil, e "não é possível desativar o último coordenador ativo" seria
    verdadeira mas não diria o que fazer.

    A revogação das sessões acontece na mesma transação da desativação: fossem
    dois commits, existiria a janela em que a pessoa está desativada e o cookie
    dela ainda vale — pequena, mas exatamente no momento em que se desativa
    alguém às pressas. `sessoes.revogar_todas` não commita, e é por isso.
    """
    agora = datetime.now(UTC)
    usuario = session.get(Usuario, usuario_id)
    if usuario is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=MENSAGEM_USUARIO_NAO_ENCONTRADO,
        )

    # `usuario_id` do principal é `None` para `X-API-Key`: a chave não tem "si
    # mesmo", e comparar `None` com o id do alvo daria falso do jeito certo.
    e_a_propria_conta = principal.usuario_id is not None and principal.usuario_id == usuario.id

    if corpo.papel is not None:
        _recusar_papel_nao_atribuivel(corpo.papel)
        if e_a_propria_conta:
            # A trava é sobre o **próprio** papel porque é o único cuja alteração
            # interessa a quem ataca: mudar o papel de outra pessoa não dá acesso
            # nenhum a quem chama. Com a trava (1) no lugar, promover-se a
            # `gestor` já é impossível — esta é a defesa em profundidade que
            # mantém isso verdadeiro se um dia a lista de papéis atribuíveis
            # mudar, e é o que faz a escalada precisar de duas contas em vez de
            # uma. De quebra evita o rebaixamento acidental, que é irreversível
            # para quem o comete: conferente não administra usuário, e voltar
            # depende de outro coordenador.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=MENSAGEM_PROPRIO_PAPEL,
            )

    if corpo.ativo is False and e_a_propria_conta:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=MENSAGEM_AUTO_DESATIVACAO,
        )

    _recusar_esvaziar_a_coordenacao(session, usuario, corpo)

    # Calculado **antes** de aplicar `corpo`: depois da atribuição não sobra
    # "valor anterior" para comparar. Compara contra o valor atual do banco, e
    # não contra "o campo veio no corpo" — um `PATCH` que reenvia o valor que já
    # está lá não é mudança de fato, e não deve gerar registro de auditoria.
    mudancas = auditoria.calcular_mudancas(usuario, corpo)

    if corpo.nome is not None:
        usuario.nome = corpo.nome
    if corpo.papel is not None:
        usuario.papel = corpo.papel.value
    if corpo.ativo is not None:
        usuario.ativo = corpo.ativo
    if corpo.ativo is False:
        # `usuarios.ativo = false` já derruba o acesso na requisição seguinte
        # (`sessoes.resolver_sessao` recusa usuário inativo), mas isso não basta:
        # sem revogar, reativar a pessoa depois **ressuscitaria** os cookies
        # antigos, inclusive o de um dispositivo que ela não tem mais. Revogar é
        # o que faz a desativação fechar as portas em vez de encostá-las.
        sessoes.revogar_todas(session, usuario.id, agora=agora)
    if mudancas:
        # Só grava quando algo mudou de fato — ver o comentário acima de
        # `calcular_mudancas`. Entra na mesma transação do `commit()` abaixo,
        # que é o requisito duro da issue #30: `auditoria.registrar` não commita
        # (ver a docstring de `auth/auditoria.py`).
        auditoria.registrar(
            session,
            usuario=principal.rotulo,
            usuario_id=principal.usuario_id,
            alvo_usuario_id=usuario.id,
            alvo_email=usuario.email,
            acao=auditoria.classificar_acao(mudancas),
            mudancas=mudancas,
        )
    session.commit()
    session.refresh(usuario)

    return UsuarioOut.model_validate(usuario)
