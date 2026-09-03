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
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
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


def revogar_todas(session: DbSession, usuario_id: uuid.UUID, *, agora: datetime) -> None:
    """Revoga **todas** as sessões abertas do usuário. **Não commita.**

    É o que fecha a recuperação de senha (issue #34): trocar a senha sem
    derrubar as sessões já abertas não resolve nada quando a conta foi
    comprometida — o invasor continua dentro com o cookie que já tem, e a pessoa
    que acabou de "recuperar" a conta acha que resolveu. Quem redefine a senha
    perde as próprias sessões junto, inclusive a do navegador de onde pediu, e
    isso é o comportamento correto: não há como saber qual das sessões abertas é
    dela.

    **Não commita, ao contrário de `revogar`**, e a diferença é deliberada: esta
    função roda dentro da transação da redefinição, junto com a senha nova e a
    marcação do token. As três entram juntas ou não entram — um commit aqui
    deixaria a pessoa deslogada com a senha antiga se a gravação seguinte
    falhasse. Quem chama commita.

    `UPDATE ... WHERE revoked_at IS NULL` num comando só, e não um `SELECT`
    seguido de laço: é uma escrita atômica, não carrega para a memória sessão
    que não vai mudar, e o `WHERE` preserva o `revoked_at` original das que já
    estavam revogadas — que é o dado de auditoria de quando aquela sessão caiu.
    """
    session.execute(
        update(Sessao)
        .where(Sessao.usuario_id == usuario_id, Sessao.revoked_at.is_(None))
        .values(revoked_at=agora)
    )
