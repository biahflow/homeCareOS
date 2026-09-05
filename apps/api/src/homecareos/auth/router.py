"""`POST /api/auth/login`, `/logout`, `GET /api/auth/eu` e a recuperação de senha.

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

A recuperação de senha (issue #34) leva o mesmo princípio ao extremo:
`POST /senha/esqueci` responde **204 sempre** — e-mail que não existe, usuário
inativo, teto de envios atingido, SMTP não configurado e até falha de envio.
Ver a docstring do endpoint.

Com MFA ativado (issue #35), o login passa a ter **duas etapas**: o primeiro
passo aceita a senha e cria uma sessão `mfa_pendente=True`, que não abre rota
nenhuma de `/api/*` (`auth/sessoes.resolver_sessao` a recusa); o segundo,
`POST /mfa/verificar`, é o único que enxerga essa sessão. Quem não tem MFA
ativado continua logando em uma etapa, com o comportamento intacto.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from homecareos.api.auth import MENSAGEM_CREDENCIAL_INVALIDA
from homecareos.auth import mfa, protecao, recuperacao, senhas, sessoes
from homecareos.auth.dependencies import principal_atual, token_de_sessao
from homecareos.auth.schema import (
    EsqueciSenhaRequest,
    LoginRequest,
    MaquinaOut,
    MfaCodigosRecuperacaoOut,
    MfaConfirmarRequest,
    MfaDesativarRequest,
    MfaIniciarOut,
    MfaPendenteOut,
    MfaReemitirCodigosRequest,
    MfaVerificarRequest,
    Principal,
    RedefinirSenhaRequest,
    UsuarioOut,
)
from homecareos.config import Settings, get_settings
from homecareos.db import cifra
from homecareos.db.models import CodigoRecuperacaoMfa, Usuario
from homecareos.db.session import get_session
from homecareos.mailer.errors import EnvioEmailError
from homecareos.mailer.provider import EmailProvider, get_email_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Mensagem do 429 de bloqueio de login. Única para trava de IP e trava de
# conta — ver a docstring do módulo e `auth/protecao.avaliar_bloqueio`.
MENSAGEM_BLOQUEIO = "muitas tentativas de login; tente novamente mais tarde"

# Mensagem do 422 da redefinição. Única para token inexistente, expirado e já
# usado: três mensagens diriam a quem tem um link velho por que ele não vale, e
# "já usado" diria que a conta existe.
MENSAGEM_TOKEN_INVALIDO = "token inválido ou expirado"

ASSUNTO_RECUPERACAO = "HomeCareOS — redefinição de senha"

# Mensagem do 422 do MFA. Única para código TOTP errado, código de recuperação
# errado e — em `/mfa/desativar` — senha errada: três mensagens diriam a quem
# está com a sessão de outra pessoa qual metade da credencial ele já tem.
MENSAGEM_MFA_CODIGO_INVALIDO = "código inválido"

# Mensagem do 409 de `/mfa/iniciar` e `/mfa/desativar`. Aqui não há o que
# esconder: quem chegou até estes endpoints já está autenticado como a própria
# pessoa, e "seu MFA já está ativo" é o que faz o frontend mostrar a tela certa.
MENSAGEM_MFA_JA_ATIVO = "o segundo fator já está ativado nesta conta"
MENSAGEM_MFA_NAO_ATIVO = "o segundo fator não está ativado nesta conta"

# Mensagem do 403 dos endpoints de gestão de MFA para `X-API-Key`. Chave de
# máquina não tem celular, não tem app autenticador e não tem segundo fator
# para configurar — ver `_usuario_da_sessao`.
MENSAGEM_MFA_SO_USUARIO = "o segundo fator é configurado por sessão de usuário"

# Mensagem do 503 de `/mfa/iniciar` sem `MFA_SECRET_KEYS` (ADR 0008). O texto
# não cita a variável para quem chama: é informação de infraestrutura, e a
# resposta da API não é o lugar de contar a configuração do servidor. Quem
# precisa do nome dela é o operador, e ele o encontra no `logger.error` que
# acompanha esta recusa e no warning do boot.
MENSAGEM_MFA_INDISPONIVEL = (
    "o segundo fator está temporariamente indisponível por configuração do servidor; "
    "avise quem administra o sistema"
)


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
    response_model=UsuarioOut | MfaPendenteOut,
    summary="Autentica por e-mail e senha e abre uma sessão",
    description=(
        "Sucesso sem MFA: cria a sessão, devolve o cookie `httpOnly` e o "
        "usuário. Sucesso com MFA ativado: cria a sessão **pendente**, devolve "
        'o cookie e `{"mfa_pendente": true}` — sem dado nenhum do usuário — e '
        "o login só se completa em `POST /api/auth/mfa/verificar`. Falha: 401 "
        "com o mesmo corpo, seja qual for o motivo. Origem com muitas falhas "
        "recentes: 429."
    ),
)
def login(
    corpo: LoginRequest,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token_anterior: Annotated[str | None, Depends(token_de_sessao)] = None,
) -> UsuarioOut | MfaPendenteOut:
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

    # Com MFA ativado, a senha correta compra apenas a sessão pendente: ela tem
    # cookie, existe no banco e não abre rota nenhuma de `/api/*` até
    # `POST /mfa/verificar` aceitar o segundo fator. A resposta não carrega dado
    # nenhum do usuário — quem parou no primeiro fator ainda não provou quem é.
    if usuario.mfa_ativado:
        _, token = sessoes.criar_sessao(
            session,
            usuario,
            duracao_horas=settings.sessao_duracao_horas,
            agora=agora,
            mfa_pendente=True,
        )
        _setar_cookie(response, settings, token)
        return MfaPendenteOut()

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


def provider_de_email(
    settings: Annotated[Settings, Depends(get_settings)],
) -> EmailProvider | None:
    """Resolve o gateway de e-mail da requisição. `None` quando não configurado.

    É uma dependency, e não uma chamada direta a `get_email_provider` dentro do
    endpoint, por uma razão só: é o que permite ao teste de integração injetar um
    dublê em memória por `app.dependency_overrides` e provar o fluxo inteiro sem
    abrir conexão SMTP nenhuma.
    """
    return get_email_provider(settings)


def _mensagem_de_recuperacao(nome: str, link: str, validade_minutos: int) -> str:
    """Corpo em texto puro do e-mail de recuperação.

    Texto puro e não HTML: é uma mensagem de quatro linhas com um link, e HTML
    aqui só acrescentaria a chance de o cliente de e-mail transformar o link em
    algo que a pessoa não consegue copiar.

    A última linha existe para quem **não** pediu: sem ela, o e-mail parece um
    aviso de que a conta foi invadida.
    """
    return (
        f"Olá, {nome}.\n\n"
        "Recebemos um pedido para redefinir a sua senha do HomeCareOS. "
        f"Abra o link abaixo para escolher uma senha nova (ele vale por "
        f"{validade_minutos} minutos e só pode ser usado uma vez):\n\n"
        f"{link}\n\n"
        "Se não foi você quem pediu, ignore este e-mail: nada muda enquanto o "
        "link não for aberto.\n"
    )


@router.post(
    "/senha/esqueci",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Envia por e-mail um link para redefinir a senha",
    description=(
        "Responde **204 sempre**, com o mesmo corpo: e-mail cadastrado ou não, "
        "usuário ativo ou não, teto de envios atingido ou não, SMTP "
        "configurado ou não."
    ),
)
def esqueci_senha(
    corpo: EsqueciSenhaRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    provider: Annotated[EmailProvider | None, Depends(provider_de_email)],
) -> None:
    """204 em todos os caminhos, e é o contrato — não uma simplificação.

    Um 404 para e-mail desconhecido entregaria a lista de quem trabalha na
    operação: bastaria alimentar o endpoint com e-mails e anotar quais
    respondem diferente. É exatamente a enumeração que a issue #30 fechou no
    login gastando uma verificação Argon2 descartável só para o tempo de
    resposta não denunciar quem existe; abri-la de novo aqui, de graça e no
    status HTTP, desfaria aquilo.

    Por isso `EnvioEmailError` também **não** vira 500: falha de SMTP só
    acontece para e-mail que existe (é o único caso em que se tenta enviar), e
    um 500 nesse caminho voltaria a dizer quem está cadastrado. Ela é registrada
    com `logger.exception` — o operador precisa saber que o gateway caiu — e a
    resposta continua 204.

    Registrado **sem** dependency de autorização, como `/login`: quem esqueceu a
    senha não tem sessão para apresentar.
    """
    agora = datetime.now(UTC)
    email = normalizar_email(corpo.email)

    usuario = session.scalars(select(Usuario).where(Usuario.email == email)).first()
    if usuario is None or not usuario.ativo:
        # Nenhum registro no banco, nenhum envio, e a mesma resposta. Quem saiu
        # da operação não recupera acesso por e-mail: reativar é decisão de
        # quem administra, não de quem clica.
        return

    if provider is None:
        # Nem emite o token: ninguém receberia o link, e o pedido ainda
        # queimaria uma vaga do teto por hora do usuário.
        logger.warning(
            "recuperação de senha desligada: SMTP não configurado "
            "(SMTP_HOST/SMTP_REMETENTE vazios). O pedido foi descartado e o "
            "caminho para redefinir a senha continua sendo o CLI."
        )
        return

    token = recuperacao.emitir_token(session, usuario, settings=settings, agora=agora)
    if token is None:
        # Teto por hora atingido. Mesma resposta: ver `recuperacao.emitir_token`.
        return

    link = f"{settings.frontend_base_url.rstrip('/')}/redefinir-senha?token={token}"
    try:
        provider.enviar(
            usuario.email,
            ASSUNTO_RECUPERACAO,
            _mensagem_de_recuperacao(usuario.nome, link, settings.senha_reset_validade_minutos),
        )
    except EnvioEmailError:
        # O token fica emitido de propósito: se o envio falhou por instabilidade
        # e a mensagem chegar atrasada, o link continua valendo.
        logger.exception("falha ao enviar o e-mail de recuperação de senha")


@router.post(
    "/senha/redefinir",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Troca a senha usando o token recebido por e-mail",
    description=(
        "Token inexistente, expirado ou já usado: 422 com a mesma mensagem. "
        "Senha fraca: 422 dizendo o requisito, **sem** consumir o token. "
        "Sucesso: 204, e todas as sessões abertas do usuário são revogadas."
    ),
)
def redefinir_senha(
    corpo: RedefinirSenhaRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Também sem dependency de autorização: quem redefine a senha não tem sessão.

    A ordem dos passos é o desenho, não acaso: a validação de força acontece
    **antes** de o token ser marcado como usado, senão digitar uma senha curta
    demais queimaria o link e obrigaria a pessoa a pedir outro e-mail.

    Tudo entra em **um commit só** — senha nova, sessões revogadas e token
    usado. Um commit por passo deixaria estados que ninguém quer explicar
    depois: senha trocada com o link ainda valendo, ou link queimado com a senha
    antiga.
    """
    agora = datetime.now(UTC)

    usuario = recuperacao.consumir_token(session, corpo.token, agora=agora)
    if usuario is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=MENSAGEM_TOKEN_INVALIDO,
        )

    try:
        senhas.validar_forca(corpo.nova_senha, minimo=settings.senha_minima_caracteres)
    except ValueError as exc:
        # A mensagem diz o requisito (ao contrário do 422 do token acima): quem
        # chegou aqui já provou, com o token, que a conta é dele.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    usuario.senha_hash = senhas.gerar_hash(corpo.nova_senha)
    # É o ponto da recuperação: se a conta foi comprometida, trocar a senha sem
    # derrubar as sessões abertas do invasor não resolve nada — ele continua
    # dentro com o cookie que já tem. Inclui as sessões da própria pessoa, e
    # está certo: não há como saber qual das abertas é dela.
    sessoes.revogar_todas(session, usuario.id, agora=agora)
    recuperacao.marcar_usado(session, corpo.token, agora=agora)
    session.commit()


# --- segundo fator (MFA por TOTP, issue #35) ---------------------------------


@router.post(
    "/mfa/verificar",
    response_model=UsuarioOut,
    summary="Completa o login apresentando o segundo fator",
    description=(
        "Lê a sessão **pendente** pelo cookie e aceita o código de seis "
        "dígitos do app autenticador ou um código de recuperação. Sucesso: a "
        "sessão deixa de ser pendente e passa a abrir `/api/*`. Falha: 401, "
        "e a tentativa conta para o bloqueio por força bruta."
    ),
)
def verificar_mfa(
    corpo: MfaVerificarRequest,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Depends(token_de_sessao)] = None,
) -> UsuarioOut:
    """Rota **pública**, e é decisão consciente.

    A credencial que ela consome é o cookie da sessão pendente, que
    `principal_atual` recusa por construção (`sessoes.resolver_sessao` devolve
    `None` para sessão pendente). Exigir `principal_atual` aqui seria exigir a
    sessão completa para poder completar a sessão — o segundo passo do login
    nunca seria alcançável. Ela está declarada em `ROTAS_PUBLICAS`, em
    `tests/test_auth.py`, com essa razão.

    **A falha registra tentativa E esta rota consulta o bloqueio da issue #33**,
    devolvendo 429 quando a conta ou a origem já estouraram o limite.

    Registrar sem consultar não bastaria, e a diferença é o que decide se o
    segundo fator vale alguma coisa. Quem chega aqui **já tem a senha** — o
    cookie pendente só existe porque o primeiro passo passou —, então o MFA é a
    última linha, e são seis dígitos. Se esta rota só contasse as falhas, elas
    trancariam o *login seguinte*, que o atacante não precisa fazer: ele já
    está com a sessão pendente na mão e pode sondar à vontade até ela expirar
    (`SESSAO_DURACAO_HORAS`, 12h por padrão). Com a consulta, a trava de conta
    corta a sondagem em `LOGIN_FALHAS_PARA_TRAVAR_CONTA` tentativas por janela.

    O atraso progressivo do login não é aplicado aqui de propósito: quem chega
    nesta rota já pagou o atraso do primeiro passo, e o que contém a sondagem é
    a trava, não o atraso.

    Sem cookie, com sessão que não é pendente, expirada, revogada ou de usuário
    desativado: **o mesmo 401** de credencial inválida. Distinguir esses casos
    diria a quem tem um cookie velho por que ele não vale.
    """
    agora = datetime.now(UTC)
    if token is None:
        raise _credencial_invalida()

    pendente = sessoes.resolver_sessao_pendente(session, token, agora=agora)
    if pendente is None:
        raise _credencial_invalida()
    sessao_pendente, usuario = pendente

    ip = protecao.ip_do_request(request, settings)

    # Antes de conferir o código, e não depois: o ponto é não deixar a sondagem
    # continuar de graça. Mesma mensagem genérica do 429 do login.
    bloqueio = protecao.avaliar_bloqueio(
        session, email=usuario.email, ip=ip, settings=settings, agora=agora
    )
    if bloqueio is not None:
        raise _bloqueado(bloqueio)

    # O TOTP primeiro, o código de recuperação só depois: o segundo é finito e
    # é a saída de quem perdeu o celular — gastá-lo enquanto o app funciona
    # seria queimar a reserva de quem digitou o código certo com um dígito
    # trocado.
    passo: int | None = None
    if usuario.mfa_secret is not None:
        passo = mfa.verificar_codigo(
            usuario.mfa_secret,
            corpo.codigo,
            agora=agora,
            janela=settings.mfa_janela_passos,
            ultimo_passo=usuario.mfa_ultimo_passo,
        )
    aceito = passo is not None
    if not aceito:
        aceito = mfa.consumir_codigo_recuperacao(session, usuario.id, corpo.codigo)

    if not aceito:
        protecao.registrar_tentativa(session, email=usuario.email, ip=ip, sucesso=False)
        session.commit()
        raise _credencial_invalida()

    # O passo aceito é gravado **antes** de a sessão ser completada, no mesmo
    # commit: é ele o anti-replay (ver `auth/mfa.verificar_codigo`). Só há passo
    # quando o caminho foi TOTP — código de recuperação não tem passo, e o seu
    # uso único já foi marcado por `consumir_codigo_recuperacao`.
    if passo is not None:
        usuario.mfa_ultimo_passo = passo
    sessao_pendente.mfa_pendente = False
    protecao.registrar_tentativa(session, email=usuario.email, ip=ip, sucesso=True)
    session.commit()
    return UsuarioOut.model_validate(usuario)


def _usuario_da_sessao(principal: Principal, session: Session) -> Usuario:
    """O usuário por trás da sessão, ou 403 para `X-API-Key`.

    Os endpoints de gestão de MFA exigem **pessoa**: chave de máquina não tem
    celular nem app autenticador, e não há segundo fator para configurar nela.
    O 403 (e não 401) é o status correto: a credencial é válida, a operação é
    que não se aplica a ela.
    """
    if principal.tipo == "maquina" or principal.usuario_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=MENSAGEM_MFA_SO_USUARIO,
        )
    usuario = session.get(Usuario, principal.usuario_id)
    if usuario is None:  # a sessão só resolve com o usuário existindo; guarda para o mypy
        raise _credencial_invalida()
    return usuario


@router.post(
    "/mfa/iniciar",
    response_model=MfaIniciarOut,
    summary="Gera o segredo TOTP para cadastrar no app autenticador",
    description=(
        "Devolve o segredo e a URI `otpauth://` do QR code. **Não ativa nada**: "
        "a ativação é `POST /api/auth/mfa/confirmar`. Com MFA já ativado: 409. "
        "Sem chave de cifra configurada no servidor: 503, e nada é gravado — o "
        "segredo nunca vai para o banco em claro (ADR 0008)."
    ),
)
def iniciar_mfa(
    principal: Annotated[Principal, Depends(principal_atual)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MfaIniciarOut:
    """Grava o segredo **sem ativar**, e chamar de novo o **substitui**.

    Gravar sem ativar é o que impede alguém de se trancar para fora: um segredo
    que o app não guardou (QR code fechado antes da hora, celular sem bateria)
    não pode passar a ser exigido no login seguinte. Enquanto `mfa_ativado` for
    `False`, o segredo não exige nada de ninguém.

    Pela mesma razão a segunda chamada substitui a primeira: quem perdeu o QR
    code no meio do cadastro precisa recomeçar, e um segredo não confirmado não
    protege nada — preservá-lo só criaria um estado impossível de sair sem
    suporte.

    Com MFA **já ativado**, 409: substituir o segredo de quem já usa o segundo
    fator, com uma sessão que pode ser sequestrada, seria trocar a credencial
    por outra sem provar nada. Desativar (com senha e código) e ativar de novo é
    o caminho.

    **Sem `MFA_SECRET_KEYS`, 503 — e nada é gravado** (ADR 0008). O segredo é
    cifrado em repouso pelo tipo da coluna (`db/cifra.SegredoCifrado`), e sem
    chave a alternativa seria gravá-lo em claro. Um sistema que degrada em
    silêncio para texto claro é pior que um que recusa: quem ativou o MFA
    achando que estava protegido não tem como descobrir que não estava.

    **503 e não 500**: é indisponibilidade de configuração do servidor, não erro
    de quem chama — a requisição está correta e passará a funcionar assim que a
    chave existir, sem ninguém mudar o cliente. A recusa vem **antes** de
    `gerar_segredo()`: não faz sentido produzir credencial que não tem onde ser
    guardada.
    """
    usuario = _usuario_da_sessao(principal, session)
    if usuario.mfa_ativado:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MENSAGEM_MFA_JA_ATIVO,
        )

    if not cifra.cifra_disponivel():
        # O nome da variável fica no log do servidor, não na resposta: quem
        # precisa dele é o operador. Ver `MENSAGEM_MFA_INDISPONIVEL`.
        logger.error(
            "POST /api/auth/mfa/iniciar recusado: %s",
            cifra.MENSAGEM_SEM_CHAVE,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MENSAGEM_MFA_INDISPONIVEL,
        )

    segredo = mfa.gerar_segredo()
    usuario.mfa_secret = segredo
    session.commit()
    return MfaIniciarOut(
        secret=segredo,
        otpauth_uri=mfa.uri_otpauth(segredo, email=usuario.email, emissor=settings.mfa_emissor),
    )


@router.post(
    "/mfa/confirmar",
    response_model=MfaCodigosRecuperacaoOut,
    summary="Ativa o segundo fator provando que o app guardou o segredo",
    description=(
        "Valida o código contra o segredo gravado por `/mfa/iniciar`, ativa o "
        "MFA e devolve os códigos de recuperação — **a única vez** em que eles "
        "existem em claro. Código errado: 422, e nada é ativado."
    ),
)
def confirmar_mfa(
    corpo: MfaConfirmarRequest,
    principal: Annotated[Principal, Depends(principal_atual)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MfaCodigosRecuperacaoOut:
    """Ativa só com a prova, e grava o passo do código usado.

    O passo vai para `mfa_ultimo_passo` no mesmo commit da ativação porque o
    anti-replay começa aqui: sem isso, o código digitado na tela de cadastro
    ainda valeria no primeiro login, e quem o tivesse visto por cima do ombro
    entraria com ele.

    Os códigos de recuperação de uma ativação anterior são apagados antes de os
    novos entrarem. Códigos de duas ativações diferentes conviverem seria uma
    lista que a pessoa não sabe que tem — e que ela não pode nem conferir,
    porque o banco só guarda o hash.
    """
    agora = datetime.now(UTC)
    usuario = _usuario_da_sessao(principal, session)
    if usuario.mfa_secret is None:
        # Nada iniciado: mesmo 422 do código errado, porque é o mesmo desfecho
        # para quem chama — não há o que confirmar.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=MENSAGEM_MFA_CODIGO_INVALIDO,
        )

    passo = mfa.verificar_codigo(
        usuario.mfa_secret,
        corpo.codigo,
        agora=agora,
        janela=settings.mfa_janela_passos,
        ultimo_passo=usuario.mfa_ultimo_passo,
    )
    if passo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=MENSAGEM_MFA_CODIGO_INVALIDO,
        )

    codigos = mfa.gerar_codigos_recuperacao(settings.mfa_codigos_recuperacao)
    session.execute(
        delete(CodigoRecuperacaoMfa).where(CodigoRecuperacaoMfa.usuario_id == usuario.id)
    )
    for codigo in codigos:
        session.add(
            CodigoRecuperacaoMfa(
                usuario_id=usuario.id,
                codigo_hash=senhas.gerar_hash(mfa.normalizar_codigo_recuperacao(codigo)),
            )
        )
    usuario.mfa_ativado = True
    usuario.mfa_ultimo_passo = passo
    session.commit()
    return MfaCodigosRecuperacaoOut(codigos=codigos)


@router.post(
    "/mfa/desativar",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desliga o segundo fator (exige senha E código atual)",
    description=(
        "Exige os **dois** fatores no corpo: senha e código. Limpa o segredo, "
        "a flag, o passo e os códigos de recuperação. O `codigo` é o do app "
        "autenticador; quando o segredo TOTP está ilegível no servidor (chave "
        "de cifra perdida ou rotacionada errado, ADR 0008), um **código de "
        "recuperação** é aceito no lugar dele. Senha ou código errado: 422 com "
        "a mesma mensagem, e a tentativa conta para o bloqueio por força bruta. "
        "MFA não ativado: 409."
    ),
)
def desativar_mfa(
    corpo: MfaDesativarRequest,
    request: Request,
    principal: Annotated[Principal, Depends(principal_atual)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Senha **e** código, e não um dos dois.

    Com só o código, uma sessão sequestrada desligaria o segundo fator sozinha —
    exatamente o que ele existe para impedir. Com só a senha, bastaria a senha
    vazada, que é a hipótese que faz alguém ativar MFA. Exigir os dois é o que
    torna a desativação tão cara quanto a invasão que ela permitiria.

    Senha errada e código errado respondem o **mesmo** 422: duas mensagens
    diriam a quem está com a sessão de outra pessoa qual metade da credencial
    ele já tem.

    **O freio da issue #33 vale aqui**, e a ausência dele era um buraco: exigir
    senha e código não adianta se o código pode ser tentado um milhão de vezes.
    Seis dígitos são 10⁶ possibilidades, e sem limite quem tivesse a senha
    chegava ao segundo fator por força bruta — sem 429, sem atraso e sem deixar
    linha em `tentativas_login`. Isso fazia desta rota um alvo mais barato que
    `/mfa/verificar`, que sempre foi protegida, e o prêmio aqui é maior:
    `/mfa/verificar` dá uma sessão, esta **desliga o segundo fator**.

    A avaliação vem antes de conferir qualquer credencial, como em
    `/mfa/verificar`, e a tentativa é registrada nos dois desfechos. A
    consequência precisa estar dita: `registrar_tentativa` grava em
    `tentativas_login` com o e-mail da pessoa, e essas linhas contam para a
    trava de conta — errar muitas vezes aqui tranca o login dela. É o preço de
    não deixar seis dígitos serem sondados de graça, e é o mesmo comportamento
    que `/mfa/verificar` já tinha.

    O 409 de MFA não ativado vem **antes** do freio: ele não olha credencial
    nenhuma e não custa Argon2, e responder 429 a quem nem tem segundo fator
    para desligar só esconderia o erro real de quem integra.

    **Segredo ilegível não é "MFA não ativado", e desde a issue #39 não responde
    mais 409.** Quando a chave de `MFA_SECRET_KEYS` se perde ou uma rotação
    remove a antiga cedo demais, `db/cifra.SegredoCifrado` degrada a leitura
    para `None` (ADR 0008) — de propósito, para o código de recuperação
    continuar logando a pessoa. Com a guarda antiga (`mfa_secret is None` →
    409), quem entrava pelo código de recuperação **não conseguia desligar o
    próprio MFA**: ficava com um segundo fator quebrado e sem saída pela API, e
    ainda recebia uma mensagem que mentia para ela — o segundo fator *está*
    ativado.

    Nesse estado a rota aceita **senha + código de recuperação** no lugar de
    senha + TOTP. Continuam sendo dois fatores, e não há degradação: o código de
    recuperação já é a credencial que pula o segundo fator no login
    (`/mfa/verificar`), então quem tem senha e código de recuperação já entra na
    conta.

    `mfa_ativado=True` com a coluna vazia **é** diagnóstico de segredo ilegível,
    e não de "nunca iniciado": os dois são limpos no mesmo commit aqui embaixo,
    e `/mfa/confirmar` só liga a flag com segredo gravado — a flag ligada com
    coluna vazia não é estado alcançável pelo fluxo normal.

    **Fica de fora, e é o caso de borda desta rota:** quem perder a chave **e**
    esgotar os códigos de recuperação continua trancado. Não existe rota
    administrativa que desative o MFA de terceiro (`auth/usuarios_router.py` não
    expõe nada de MFA, por decisão do ADR 0004), então a saída é intervenção
    direta no banco. Criar essa rota é decisão de outra natureza — quem
    administra usuário passaria a poder desligar o segundo fator de outra
    pessoa —, e não foi tomada aqui.

    O segredo é apagado junto com a flag, e não guardado "para o caso de
    religar": segredo órfão de MFA desligado só serve para vazar num dump
    depois. Religar é `POST /mfa/iniciar` de novo, com um segredo novo.
    """
    agora = datetime.now(UTC)
    usuario = _usuario_da_sessao(principal, session)
    # Só a flag. Segredo ilegível cai nos caminhos de baixo, não aqui — ver a
    # docstring.
    if not usuario.mfa_ativado:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MENSAGEM_MFA_NAO_ATIVO,
        )

    ip = protecao.ip_do_request(request, settings)
    bloqueio = protecao.avaliar_bloqueio(
        session, email=usuario.email, ip=ip, settings=settings, agora=agora
    )
    if bloqueio is not None:
        raise _bloqueado(bloqueio)

    senha_ok = senhas.verificar(usuario.senha_hash, corpo.senha)
    if usuario.mfa_secret is not None:
        # Caminho de sempre. Os dois são conferidos mesmo quando o primeiro
        # falha: sair no primeiro erro faria o tempo de resposta dizer se a
        # senha estava certa, e verificar TOTP não consome nada.
        passo = mfa.verificar_codigo(
            usuario.mfa_secret,
            corpo.codigo,
            agora=agora,
            janela=settings.mfa_janela_passos,
            ultimo_passo=usuario.mfa_ultimo_passo,
        )
        aceito = senha_ok and passo is not None
    else:
        # Segredo ilegível: o código de recuperação faz o papel do TOTP. Aqui a
        # ordem é o oposto da de cima, e é deliberada:
        # `mfa.consumir_codigo_recuperacao` **consome** (uso único), então
        # conferi-lo antes da senha faria uma senha digitada errada queimar um
        # código de uma lista finita — a última reserva de quem já está sem o
        # segundo fator. O que isso custa é o tempo de resposta distinguir senha
        # certa de senha errada; é troca consciente, e não entrega capacidade
        # nova: `POST /api/auth/login` já responde 200 ou 401 para a mesma
        # pergunta, sob o mesmo freio de força bruta. O que **não** muda é o que
        # quem chama observa: os dois desfechos são o mesmo 422 abaixo.
        aceito = senha_ok and mfa.consumir_codigo_recuperacao(session, usuario.id, corpo.codigo)

    if not aceito:
        protecao.registrar_tentativa(session, email=usuario.email, ip=ip, sucesso=False)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=MENSAGEM_MFA_CODIGO_INVALIDO,
        )

    usuario.mfa_ativado = False
    usuario.mfa_secret = None
    usuario.mfa_ultimo_passo = None
    # Com o segredo ilegível, `usuario.mfa_secret` **já lê `None`** (a degradação
    # de `db/cifra.SegredoCifrado`), e atribuir `None` sobre `None` não é
    # mudança nenhuma para o SQLAlchemy: a coluna ficaria fora do UPDATE e o
    # token que não abre continuaria no banco depois de o MFA ter sido
    # desligado — exatamente o segredo órfão que o parágrafo acima recusa
    # guardar. `flag_modified` é o que força o `NULL` a acontecer de verdade.
    flag_modified(usuario, "mfa_secret")
    session.execute(
        delete(CodigoRecuperacaoMfa).where(CodigoRecuperacaoMfa.usuario_id == usuario.id)
    )
    # O sucesso é registrado como em `/mfa/verificar`: esta é reautenticação
    # completa (senha **e** código), e zerar o contador evita que erros de
    # digitação anteriores continuem pesando contra quem acabou de provar quem é.
    protecao.registrar_tentativa(session, email=usuario.email, ip=ip, sucesso=True)
    session.commit()


@router.post(
    "/mfa/reemitir-codigos",
    response_model=MfaCodigosRecuperacaoOut,
    summary="Emite uma lista nova de códigos de recuperação (exige senha E código atual)",
    description=(
        "Substitui **todos** os códigos de recuperação da conta — usados e não "
        "usados — por uma lista nova, devolvida em claro **uma única vez**. O "
        "segredo TOTP não muda: o app autenticador cadastrado continua valendo. "
        "Exige os **dois** fatores no corpo: senha e código do app. Senha ou "
        "código errado: 422 com a mesma mensagem, e a tentativa conta para o "
        "bloqueio por força bruta. MFA não ativado: 409."
    ),
)
def reemitir_codigos_mfa(
    corpo: MfaReemitirCodigosRequest,
    request: Request,
    principal: Annotated[Principal, Depends(principal_atual)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MfaCodigosRecuperacaoOut:
    """Lista nova de códigos de recuperação, sem desativar o segundo fator (issue #39).

    Até aqui, quem perdia a lista tinha um caminho só: desativar e ativar de
    novo — segredo novo, QR code novo, app reconfigurado. O custo era alto o
    bastante para a pessoa adiar, e adiar significa ficar sem a saída de
    emergência justamente enquanto o MFA está ligado.

    **Reautenticação, e não "a sessão está válida".** O que sai daqui é
    credencial de *bypass permanente* do segundo fator: quem tem um destes
    códigos entra sem o app autenticador. Uma sessão sequestrada que pudesse
    chamar esta rota viraria acesso permanente à conta, imune à troca de senha
    e ao próprio MFA. Por isso ela exige senha **e** código atual, exatamente
    como `/mfa/desativar` — as duas operações têm o mesmo peso.

    Senha errada e código errado respondem o **mesmo** 422, pela mesma razão de
    `/mfa/desativar`: duas mensagens diriam a quem está com a sessão de outra
    pessoa qual metade da credencial ele já tem.

    O `codigo` é só o TOTP: código de recuperação **não** é aceito aqui (ver
    `MfaReemitirCodigosRequest`). Um código vazado que gerasse oito novos
    desfaria o "uso único" da lista inteira.

    **Todos os códigos antigos morrem, usados e não usados**, no mesmo `delete`
    que precede o `insert` — é o mesmo par de `confirmar_mfa`, e na mesma
    transação. Preservar um código antigo faria a reemissão *aumentar* a
    superfície de ataque: quem quer trocar a lista porque ela pode ter vazado
    ficaria com a lista vazada valendo do mesmo jeito. A atomicidade também não
    é detalhe — apagar e falhar antes de gravar deixaria a pessoa sem código
    nenhum e sem saber disso.

    O segredo TOTP **não** é tocado: reemitir código de recuperação não é
    rotacionar o segundo fator, e obrigar a reconfigurar o app seria devolver o
    custo que esta rota existe para eliminar.

    O passo TOTP aceito vai para `mfa_ultimo_passo` no mesmo commit, como em
    `/mfa/verificar` e `/mfa/confirmar`: o código gasto aqui não pode servir
    para o login em seguida. (`/mfa/desativar` não grava o passo porque zera o
    campo — lá não sobra segundo fator para replicar.)

    **O freio da issue #33 vale aqui**, consultado antes de qualquer Argon2 e
    registrado nos dois desfechos, como em `/mfa/verificar`. A consequência
    precisa estar dita: `registrar_tentativa` grava em `tentativas_login` com o
    e-mail da pessoa, e essas linhas contam para a trava de conta — **errar a
    senha ou o código muitas vezes aqui tranca o login dela**. É o preço de não
    deixar seis dígitos serem sondados de graça numa rota que emite bypass do
    segundo fator, e é o mesmo comportamento que `/mfa/verificar` já tem.

    O 409 de MFA não ativado vem **antes** do freio de propósito: ele não olha
    credencial nenhuma e não custa Argon2, e quem chama já sabe o estado da
    própria conta — responder 429 a quem nem tem o que reemitir só esconderia o
    erro real de quem integra.

    **Segredo ilegível continua respondendo 409 aqui, e isso não é esquecimento.**
    `/mfa/desativar` passou a aceitar senha + código de recuperação nesse estado
    (issue #39); esta rota **não**, e a diferença é o que cada uma entrega. Com o
    segredo ilegível o segundo fator está quebrado: emitir oito códigos novos
    para um MFA que não gera código nenhum não devolve o app autenticador a
    ninguém — só produz mais credencial de bypass para uma conta que já não tem
    fator para pular. O caminho é desligar (`/mfa/desativar`) e religar
    (`/mfa/iniciar`), com segredo novo e lista nova.
    """
    agora = datetime.now(UTC)
    usuario = _usuario_da_sessao(principal, session)
    if not usuario.mfa_ativado or usuario.mfa_secret is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MENSAGEM_MFA_NAO_ATIVO,
        )

    ip = protecao.ip_do_request(request, settings)
    bloqueio = protecao.avaliar_bloqueio(
        session, email=usuario.email, ip=ip, settings=settings, agora=agora
    )
    if bloqueio is not None:
        raise _bloqueado(bloqueio)

    # Os dois são conferidos sempre, e o desfecho é decidido depois: sair no
    # primeiro erro faria o tempo de resposta dizer se a senha estava certa.
    senha_ok = senhas.verificar(usuario.senha_hash, corpo.senha)
    passo = mfa.verificar_codigo(
        usuario.mfa_secret,
        corpo.codigo,
        agora=agora,
        janela=settings.mfa_janela_passos,
        ultimo_passo=usuario.mfa_ultimo_passo,
    )
    if not senha_ok or passo is None:
        protecao.registrar_tentativa(session, email=usuario.email, ip=ip, sucesso=False)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=MENSAGEM_MFA_CODIGO_INVALIDO,
        )

    codigos = mfa.gerar_codigos_recuperacao(settings.mfa_codigos_recuperacao)
    session.execute(
        delete(CodigoRecuperacaoMfa).where(CodigoRecuperacaoMfa.usuario_id == usuario.id)
    )
    for codigo in codigos:
        session.add(
            CodigoRecuperacaoMfa(
                usuario_id=usuario.id,
                codigo_hash=senhas.gerar_hash(mfa.normalizar_codigo_recuperacao(codigo)),
            )
        )
    usuario.mfa_ultimo_passo = passo
    protecao.registrar_tentativa(session, email=usuario.email, ip=ip, sucesso=True)
    session.commit()
    return MfaCodigosRecuperacaoOut(codigos=codigos)
