"""`POST /api/auth/login`, `POST /api/auth/logout` e `GET /api/auth/eu`.

`POST /login` é a única rota de `/api/*` que nasce **sem** exigir credencial, e
a razão é a que parece: não dá para exigir sessão para criar sessão. Ela é
registrada em `main.py` sem a dependency de autorização — a exceção está
declarada lá também, para ninguém "consertar" isso mais tarde.

Ele mesmo assim lê o cookie de sessão, quando existe: relogar com sucesso no
mesmo navegador revoga a sessão anterior antes de criar a nova. Sem isso, cada
relogin deixaria para trás uma sessão órfã — válida até expirar, sem cookie que
a apresente e sem ninguém para revogá-la.

A revogação acontece **depois** de a credencial ser aceita, nunca na entrada:
revogar antes faria uma senha digitada errada derrubar a sessão válida de quem
já estava dentro.

O 401 do login é **igual** para e-mail inexistente, senha errada e usuário
inativo, e o caminho do e-mail inexistente ainda gasta uma verificação Argon2
descartável (`senhas.verificar_dummy`). Os dois cuidados existem pelo mesmo
motivo: nem o corpo da resposta nem o tempo dela podem dizer quem está
cadastrado.

Antes de qualquer uma dessas checagens, o login consulta `auth/protecao.py`
(issue #33): freio contra força bruta, com atraso progressivo, trava de IP e
trava de conta. O bloqueio é avaliado **antes** de qualquer consulta ao
usuário e antes de qualquer Argon2 — o ponto é não gastar CPU com quem já
estourou o limite. A resposta de bloqueio (429) é genérica e idêntica para
trava de IP e trava de conta, pelo mesmo motivo do 401 acima: duas mensagens
diferentes diriam a quem sonda se a conta existe.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from homecareos.api.auth import MENSAGEM_CREDENCIAL_INVALIDA
from homecareos.auth import protecao, senhas, sessoes
from homecareos.auth.dependencies import principal_atual, token_de_sessao
from homecareos.auth.schema import LoginRequest, MaquinaOut, Principal, UsuarioOut
from homecareos.config import Settings, get_settings
from homecareos.db.models import Usuario
from homecareos.db.session import get_session

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Mensagem do 429 de bloqueio de login. Única para trava de IP e trava de
# conta — ver a docstring do módulo e `auth/protecao.avaliar_bloqueio`.
MENSAGEM_BLOQUEIO = "muitas tentativas de login; tente novamente mais tarde"


def normalizar_email(email: str) -> str:
    """E-mail em minúsculas, sem espaço nas pontas — na escrita e na busca.

    A normalização precisa ser a mesma nos dois lados: cadastrar `Ana@x.com` e
    procurar por `ana@x.com` só encontra a mesma pessoa porque as duas pontas
    passam por aqui.
    """
    return email.strip().lower()


def _setar_cookie(response: Response, settings: Settings, token: str) -> None:
    """Escreve o cookie de sessão com as flags que fazem dele uma credencial.

    - `httponly=True`: JavaScript da página não lê o token. Um XSS continua
      sendo grave, mas não sai carregando a sessão embaixo do braço.
    - `samesite="lax"`: o cookie não acompanha requisição cross-site iniciada
      por outro domínio, que é o que o CSRF explora.
    - `secure` **condicional**, e não fixo, porque em `local` a API roda em
      `http://localhost` e o navegador simplesmente não envia cookie `Secure`
      por HTTP — o desenvolvimento inteiro pararia. Fora de `local` ele é
      obrigatório: sem ele o token trafega em claro na rede. Esta é a linha que
      alguém "simplifica" para `secure=False` e leva para produção; é para isso
      que este comentário existe.
    """
    response.set_cookie(
        key=settings.sessao_cookie_nome,
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=settings.environment != "local",
        max_age=settings.sessao_duracao_horas * 3600,
    )


@router.post(
    "/login",
    response_model=UsuarioOut,
    summary="Autentica por e-mail e senha e abre uma sessão",
    description=(
        "Sucesso: cria a sessão, devolve o cookie `httpOnly` e o usuário. "
        "Falha: 401 com o mesmo corpo, seja qual for o motivo. Origem com "
        "muitas falhas recentes: 429."
    ),
)
def login(
    corpo: LoginRequest,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token_anterior: Annotated[str | None, Depends(token_de_sessao)] = None,
) -> UsuarioOut:
    agora = datetime.now(UTC)
    email = normalizar_email(corpo.email)
    ip = protecao.ip_do_request(request, settings)

    # Antes de qualquer consulta ao usuário e antes de qualquer Argon2: o
    # ponto do bloqueio é não gastar CPU com quem já estourou o limite.
    bloqueio = protecao.avaliar_bloqueio(
        session, email=email, ip=ip, settings=settings, agora=agora
    )
    if bloqueio is not None:
        raise _bloqueado(bloqueio)

    usuario = session.scalars(select(Usuario).where(Usuario.email == email)).first()
    if usuario is None:
        # Gasta o tempo de uma verificação Argon2 mesmo sem ter o que verificar:
        # sem isto o e-mail inexistente responde ordens de grandeza mais rápido
        # que a senha errada, e o cronômetro vira uma lista de quem trabalha
        # aqui. Ver `senhas.verificar_dummy`.
        senhas.verificar_dummy()
        _registrar_falha_e_atrasar(session, email=email, ip=ip, settings=settings, agora=agora)
        raise _credencial_invalida()
    if not senhas.verificar(usuario.senha_hash, corpo.senha):
        _registrar_falha_e_atrasar(session, email=email, ip=ip, settings=settings, agora=agora)
        raise _credencial_invalida()
    if not usuario.ativo:
        # Mesma resposta de senha errada, e não um "usuário desativado": quem
        # saiu da operação não precisa ser anunciado a quem estiver sondando.
        _registrar_falha_e_atrasar(session, email=email, ip=ip, settings=settings, agora=agora)
        raise _credencial_invalida()

    # No sucesso, a tentativa é registrada mas não commitada aqui: o commit de
    # `sessoes.criar_sessao`, logo abaixo, cobre esta linha também.
    protecao.registrar_tentativa(session, email=email, ip=ip, sucesso=True)

    # Só agora, e nunca antes de a credencial ser aceita: revogar logo na entrada
    # faria uma senha digitada errada derrubar a sessão válida de quem já estava
    # dentro. `samesite="lax"` impede que um site de terceiros dispare esse POST
    # com o cookie da vítima, mas dentro da própria origem o efeito é real — e
    # ser deslogado por errar a senha uma vez é comportamento que ninguém espera.
    if token_anterior is not None:
        sessoes.revogar(session, token_anterior, agora=agora)

    _, token = sessoes.criar_sessao(
        session, usuario, duracao_horas=settings.sessao_duracao_horas, agora=agora
    )
    _setar_cookie(response, settings, token)
    return UsuarioOut.model_validate(usuario)


def _registrar_falha_e_atrasar(
    session: Session, *, email: str, ip: str, settings: Settings, agora: datetime
) -> None:
    """Registra a falha, commita, e dorme o atraso progressivo. Nesta ordem.

    O commit precisa acontecer **antes** do `time.sleep`: senão duas
    requisições paralelas leem o mesmo contador (`falhas_recentes`) e o
    atraso não progride. `time.sleep`, e não `asyncio.sleep`: o endpoint é
    síncrono e roda no threadpool do FastAPI.
    """
    protecao.registrar_tentativa(session, email=email, ip=ip, sucesso=False)
    session.commit()
    falhas = protecao.falhas_recentes(session, email=email, ip=ip, settings=settings, agora=agora)
    atraso = protecao.calcular_atraso(
        falhas,
        base=settings.login_atraso_base_segundos,
        maximo=settings.login_atraso_maximo_segundos,
    )
    time.sleep(atraso)


def _credencial_invalida() -> HTTPException:
    """O único 401 do login. Um lugar só, para os três casos não divergirem."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=MENSAGEM_CREDENCIAL_INVALIDA,
    )


def _bloqueado(bloqueio: protecao.Bloqueio) -> HTTPException:
    """O único 429 do login. Mensagem genérica — ver `MENSAGEM_BLOQUEIO`."""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=MENSAGEM_BLOQUEIO,
        headers={"Retry-After": str(bloqueio.segundos_restantes)},
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoga a sessão do cookie e apaga o cookie",
    description="Idempotente: sem cookie, ou com cookie já revogado, responde 204 do mesmo jeito.",
)
def logout(
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Depends(token_de_sessao)] = None,
) -> None:
    """Sem credencial exigida, de propósito.

    Um logout que respondesse 401 para cookie expirado deixaria o navegador
    preso com um cookie que ele não consegue nem descartar. Revogar o que não
    existe mais não muda nada; apagar o cookie sempre é o que o cliente precisa.
    """
    if token is not None:
        sessoes.revogar(session, token, agora=datetime.now(UTC))
    response.delete_cookie(
        key=settings.sessao_cookie_nome,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.environment != "local",
    )


@router.get(
    "/eu",
    response_model=UsuarioOut | MaquinaOut,
    summary="Quem está autenticado nesta requisição",
    description=(
        "Com sessão de usuário, devolve o usuário. Com `X-API-Key`, devolve "
        '`{"tipo": "maquina"}` — não há pessoa por trás da chave. Sem '
        "credencial, 401."
    ),
)
def eu(
    principal: Annotated[Principal, Depends(principal_atual)],
    session: Annotated[Session, Depends(get_session)],
) -> UsuarioOut | MaquinaOut:
    if principal.tipo == "maquina" or principal.usuario_id is None:
        return MaquinaOut()

    usuario = session.get(Usuario, principal.usuario_id)
    if usuario is None:  # a sessão só resolve com o usuário existindo; guarda para o mypy
        raise _credencial_invalida()
    return UsuarioOut.model_validate(usuario)
