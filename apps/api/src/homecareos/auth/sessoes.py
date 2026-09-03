"""Ciclo de vida da sessão de usuário: criar, resolver e revogar.

O token é gerado com `secrets.token_urlsafe(32)` — 256 bits de entropia de um
gerador criptográfico, e não `uuid4` nem `random`: o token é a credencial
inteira, quem o tiver é a pessoa.

Ele é devolvido **uma única vez**, no retorno de `criar_sessao`, e não é
recuperável depois — o banco guarda só o SHA-256 (ver a docstring de
`db/models/sessao.py`). Se alguém perder o token, o caminho é logar de novo, e
isso é a propriedade que se quer, não um incômodo.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from homecareos.db.models import Sessao, Usuario


def hash_do_token(token: str) -> str:
    """SHA-256 hexadecimal do token — a forma em que a sessão é guardada e buscada."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def criar_sessao(
    session: DbSession, usuario: Usuario, *, duracao_horas: int, agora: datetime
) -> tuple[Sessao, str]:
    """Cria a sessão e devolve `(sessao, token)`. Commita.

    O token do retorno é a **única** vez que ele existe em claro fora do cookie:
    quem chama o entrega ao navegador e o esquece.
    """
    token = secrets.token_urlsafe(32)
    sessao = Sessao(
        usuario_id=usuario.id,
        token_hash=hash_do_token(token),
        expires_at=agora + timedelta(hours=duracao_horas),
    )
    session.add(sessao)
    session.commit()
    session.refresh(sessao)
    return sessao, token


def resolver_sessao(session: DbSession, token: str, *, agora: datetime) -> Usuario | None:
    """Devolve o usuário da sessão, ou `None` quando ela não vale mais.

    `None` cobre, sem distinguir, cinco casos: token que não existe, sessão
    expirada, sessão revogada, usuário apagado e **usuário desativado**.

    O último é metade da razão de a sessão ter estado no banco: desligar alguém
    tem que derrubar o acesso na hora, e não quando o token dele vencer. Um JWT
    autocontido só conseguiria isso com uma denylist — que é o mesmo estado que
    ele prometia evitar (ADR 0001).
    """
    # Um `join` só, e não duas consultas: a sessão sem o usuário não decide
    # nada — `ativo` faz parte da validade da sessão, e é ele que faz desativar
    # alguém derrubar o acesso na requisição seguinte.
    linha = session.execute(
        select(Sessao, Usuario)
        .join(Usuario, Usuario.id == Sessao.usuario_id)
        .where(Sessao.token_hash == hash_do_token(token))
    ).first()
    if linha is None:
        return None

    sessao, usuario = linha.tuple()
    if sessao.revoked_at is not None or sessao.expires_at <= agora:
        return None
    if not usuario.ativo:
        return None
    return usuario


def revogar(session: DbSession, token: str, *, agora: datetime) -> None:
    """Marca a sessão do token como revogada. Idempotente e silenciosa. Commita.

    Token desconhecido ou sessão já revogada não é erro: o logout precisa
    funcionar com cookie velho, cookie de outra instalação e cookie nenhum —
    quem já não tem sessão já está deslogado.
    """
    sessao = session.scalars(
        select(Sessao).where(Sessao.token_hash == hash_do_token(token))
    ).first()
    if sessao is None or sessao.revoked_at is not None:
        return
    sessao.revoked_at = agora
    session.commit()
