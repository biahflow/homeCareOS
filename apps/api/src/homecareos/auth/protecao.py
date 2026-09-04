"""Freio para força bruta contra `POST /api/auth/login` — issue #33.

Hoje o único custo de uma tentativa é ~35 ms de Argon2 (`auth/senhas.py`), e é
só isso que separa um atacante de um dicionário inteiro. Defesa em
profundidade, três mecanismos independentes:

1. **atraso progressivo** por conta+IP — dificulta sem travar ninguém;
2. **trava de IP** após N falhas na janela — contém sonda contra várias contas
   vinda da mesma origem;
3. **trava de conta**, num limiar bem mais alto, como último recurso — contém
   sonda contra uma conta específica vinda de várias origens.

**A contagem é sempre pela string de e-mail tentada, exista ou não a conta.**
A issue #30 gastou uma verificação Argon2 descartável (`senhas.verificar_dummy`)
só para fechar o vazamento por tempo; contar só para e-mail cadastrado
reabriria o mesmo vazamento por outro caminho — ver a docstring de
`db/models/tentativa_login.py`.

**O atraso tem teto, e o teto não é ajuste fino.** O endpoint de login é
síncrono e roda no threadpool do FastAPI: dormir ocupa uma thread do pool.
Sem teto, requisições baratas (que só leem o contador, sem gastar Argon2)
esgotariam o pool sozinhas, e o próprio atraso viraria o vetor de negação de
serviço — ver `calcular_atraso`.

**IP atrás de proxy não é confiável por padrão.** `request.client.host` é o IP
do balanceador em qualquer deploy sério, e confiar em `X-Forwarded-For` sem
saber que existe um proxy que o reescreve deixaria o atacante forjar o header
e escapar da trava de IP — ver `ip_do_request` e
`config.confiar_em_x_forwarded_for`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from fastapi import Request
from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from homecareos.config import Settings
from homecareos.db.models.tentativa_login import TentativaLogin


@dataclass(frozen=True)
class Bloqueio:
    """Resultado de `avaliar_bloqueio` quando a requisição não pode prosseguir.

    `motivo` é uso interno (log/depuração/teste) — nunca vaza para a resposta:
    a mensagem ao cliente é genérica e igual para os dois motivos (ver
    `auth/router.py`), porque duas mensagens diferentes diriam a quem sonda se
    a conta existe.
    """

    motivo: str
    segundos_restantes: int


def ip_do_request(request: Request, settings: Settings) -> str:
    """IP de origem da requisição, segundo a política de confiança em proxy.

    Por padrão (`confiar_em_x_forwarded_for=False`) o header é ignorado e a
    origem é `request.client.host`. Só com a configuração ligada — e só então
    — o **primeiro** elemento de `X-Forwarded-For` é usado: é o cliente
    original, os elementos seguintes são cada proxy que a requisição
    atravessou.

    `request.client` pode ser `None` (ASGI sem peer, ex.: alguns transports de
    teste): devolve `"desconhecido"` em vez de levantar — o login não pode
    quebrar por não saber de onde veio a requisição.
    """
    if settings.confiar_em_x_forwarded_for:
        cabecalho = request.headers.get("x-forwarded-for")
        if cabecalho:
            return cabecalho.split(",")[0].strip()
    if request.client is None:
        return "desconhecido"
    return request.client.host


def calcular_atraso(falhas: int, *, base: float, maximo: float) -> float:
    """Atraso em segundos antes de responder a uma falha de login, com TETO.

    Zero falhas ainda registradas: `0.0`, não há o que atrasar. Daí em diante,
    backoff exponencial (`base * 2 ** (falhas - 1)`) até `maximo` — o teto é o
    que impede requisições baratas de esgotar o threadpool síncrono do
    FastAPI e transformar o próprio atraso em ataque de negação de serviço.

    Função **pura**, sem I/O: é o que permite provar o teto (ex.: 50 falhas)
    num teste unitário instantâneo, sem o teste precisar dormir de verdade.
    """
    if falhas <= 0:
        return 0.0
    return min(base * (2.0 ** (falhas - 1)), maximo)


def _ultimo_sucesso(
    session: Session, *, email: str | None = None, ip: str | None = None
) -> datetime | None:
    """`created_at` do sucesso mais recente que bate com o filtro, ou `None`."""
    condicoes: list[ColumnElement[bool]] = [TentativaLogin.sucesso.is_(True)]
    if email is not None:
        condicoes.append(TentativaLogin.email_tentado == email)
    if ip is not None:
        condicoes.append(TentativaLogin.ip == ip)
    return session.scalar(select(func.max(TentativaLogin.created_at)).where(*condicoes))


def _contar_falhas(
    session: Session,
    *,
    email: str | None = None,
    ip: str | None = None,
    desde: datetime,
    depois_do_sucesso: datetime | None,
) -> int:
    """Falhas que batem com o filtro, dentro da janela (`created_at >= desde`)
    e — quando `depois_do_sucesso` é informado — só as que vieram depois dele.
    """
    condicoes: list[ColumnElement[bool]] = [
        TentativaLogin.sucesso.is_(False),
        TentativaLogin.created_at >= desde,
    ]
    if email is not None:
        condicoes.append(TentativaLogin.email_tentado == email)
    if ip is not None:
        condicoes.append(TentativaLogin.ip == ip)
    if depois_do_sucesso is not None:
        condicoes.append(TentativaLogin.created_at > depois_do_sucesso)
    total = session.scalar(select(func.count()).select_from(TentativaLogin).where(*condicoes))
    return total or 0


def _houve_sucesso(session: Session, *, ip: str, desde: datetime) -> bool:
    """Existe algum login bem-sucedido deste `ip` dentro da janela?

    É a evidência de que há gente trabalhando por trás daquele endereço — ver
    a regra 1 de `avaliar_bloqueio`.
    """
    return (
        session.scalar(
            select(TentativaLogin.id)
            .where(
                TentativaLogin.ip == ip,
                TentativaLogin.sucesso.is_(True),
                TentativaLogin.created_at >= desde,
            )
            .limit(1)
        )
        is not None
    )


def avaliar_bloqueio(
    session: Session, *, email: str, ip: str, settings: Settings, agora: datetime
) -> Bloqueio | None:
    """`None` quando o login pode prosseguir; `Bloqueio` quando não. Nesta ordem:

    1. **IP travado**: falhas daquele `ip` na janela `>= login_falhas_para_travar_ip`
       **E nenhum login bem-sucedido daquele `ip` na mesma janela.**

       A segunda condição não é refinamento, é o que impede o mecanismo de
       derrubar a operação. Um IP compartilhado é o caso comum, não a exceção:
       atrás de proxy — e o default é `confiar_em_x_forwarded_for=False` — a
       empresa inteira chega com um IP só. Contando falhas cruas, dez erros de
       digitação somados de toda a equipe em quinze minutos trancariam todo
       mundo, e no pior momento: o começo do turno, quando todos logam juntos.

       Exigir zero sucessos separa os dois casos pelo que de fato os distingue.
       Uma rede com gente trabalhando tem logins que funcionam; quem está
       sondando senha não tem nenhum. O atacante que já possui uma credencial
       válida escapa desta regra — e é aceitável: ele já não é o caso que a
       trava de IP existe para conter, e as defesas por conta (atraso
       progressivo e trava de conta) continuam valendo sobre ele.
    2. **Conta travada**: falhas daquele `email_tentado` na janela, contadas
       só depois do último sucesso **daquele e-mail**, `>= login_falhas_para_travar_conta`
       — um limiar bem mais alto que o de IP, de propósito: travar conta é o
       último recurso, porque permite que qualquer um que saiba o e-mail de
       alguém mantenha essa pessoa fora do sistema de propósito.
    """
    inicio_janela = agora - timedelta(minutes=settings.login_janela_minutos)

    falhas_ip = _contar_falhas(session, ip=ip, desde=inicio_janela, depois_do_sucesso=None)
    if falhas_ip >= settings.login_falhas_para_travar_ip and not _houve_sucesso(
        session, ip=ip, desde=inicio_janela
    ):
        return Bloqueio(motivo="ip", segundos_restantes=settings.login_trava_minutos * 60)

    ultimo_sucesso_email = _ultimo_sucesso(session, email=email)
    falhas_conta = _contar_falhas(
        session, email=email, desde=inicio_janela, depois_do_sucesso=ultimo_sucesso_email
    )
    if falhas_conta >= settings.login_falhas_para_travar_conta:
        return Bloqueio(motivo="conta", segundos_restantes=settings.login_trava_minutos * 60)

    return None


def registrar_tentativa(session: Session, *, email: str, ip: str, sucesso: bool) -> None:
    """Adiciona a linha à sessão. **Não commita** — quem chama decide quando.

    No caminho de falha (`auth/router.py`), o commit precisa acontecer *antes*
    do `time.sleep` do atraso: senão duas requisições paralelas leem o mesmo
    contador (via `falhas_recentes`) e o atraso não progride. No caminho de
    sucesso, o commit de `sessoes.criar_sessao` já cobre esta linha.
    """
    session.add(TentativaLogin(email_tentado=email, ip=ip, sucesso=sucesso))


def falhas_recentes(
    session: Session, *, email: str, ip: str, settings: Settings, agora: datetime
) -> int:
    """Falhas da combinação `email` + `ip`, na janela, depois do último sucesso
    dessa mesma combinação — é o contador que alimenta o atraso progressivo.

    Login bem-sucedido zera o estado daquela conta+IP: a próxima falha reinicia
    a progressão do zero, em vez de continuar de onde o atacante parou.
    """
    inicio_janela = agora - timedelta(minutes=settings.login_janela_minutos)
    ultimo_sucesso = _ultimo_sucesso(session, email=email, ip=ip)
    return _contar_falhas(
        session, email=email, ip=ip, desde=inicio_janela, depois_do_sucesso=ultimo_sucesso
    )


def limpar_tentativas_antigas(
    session: Session, *, antes_de: datetime, lote: int = 1000, dry_run: bool = False
) -> int:
    """Apaga tentativas com `created_at < antes_de` e devolve quantas saíram (ou
    sairiam, em `dry_run`, sem tocar o banco). Commita a cada lote de até
    `lote` linhas.

    Não há agendador nesta issue: esta função existe para ser chamada por um
    cron futuro (ou manualmente) — ver `retencao/cli.py` (issue #39) e o
    README. `lote=1000` é o mesmo default de `Settings.retencao_tamanho_lote`;
    existe aqui só para permitir chamar a função sem toda a configuração, como
    faz o teste existente.

    Um `DELETE` único sobre anos de tentativas acumuladas seguraria locks e
    cresceria o WAL de uma vez, numa tabela que recebe insert a cada login —
    daí o lote, com commit a cada um. O preço é que a operação inteira deixa
    de ser atômica: uma interrupção no meio apaga só parte, e a próxima
    execução termina o serviço.
    """
    condicao = TentativaLogin.created_at < antes_de
    if dry_run:
        total = session.scalar(select(func.count()).select_from(TentativaLogin).where(condicao))
        return total or 0

    total = 0
    while True:
        subquery = select(TentativaLogin.id).where(condicao).limit(lote)
        # `Session.execute` é tipado como `Result[Any]`; em tempo de execução um
        # `delete()` sempre devolve `CursorResult`, que é quem tem `.rowcount`.
        resultado = cast(
            "CursorResult[Any]",
            session.execute(delete(TentativaLogin).where(TentativaLogin.id.in_(subquery))),
        )
        session.commit()
        apagadas = resultado.rowcount
        total += apagadas
        if apagadas < lote:
            break
    return total
