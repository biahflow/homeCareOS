"""Ciclo de vida do token de recuperação de senha: emitir, resolver e marcar usado.

O token é gerado com `secrets.token_urlsafe(32)` — 256 bits de entropia de um
gerador criptográfico, como o da sessão (`auth/sessoes.py`), e pelo mesmo
motivo: quem o tiver **é** a pessoa. Aqui vale ainda mais, porque este token não
dá acesso: dá a senha.

Ele é devolvido **uma única vez**, no retorno de `emitir_token`, e não é
recuperável depois — o banco guarda só o SHA-256 (ver
`db/models/token_recuperacao.py`). Quem perder o link pede outro e-mail.

**Emitir e marcar usado são passos separados, e é de propósito.** A redefinição
valida a força da senha nova *depois* de resolver o token e *antes* de marcá-lo
usado: consumir o token numa validação que falhou obrigaria a pessoa a pedir
outro e-mail por ter digitado uma senha curta demais — que é exatamente o
momento em que ela já está com pressa e sem paciência. Ver `auth/router.py`.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from homecareos.config import Settings
from homecareos.db.models import TokenRecuperacao, Usuario

# Janela do freio de emissão. Uma hora é o que `senha_reset_max_por_hora`
# nomeia; a constante existe para o nome da configuração e a consulta não
# poderem divergir em silêncio.
JANELA_DO_TETO = timedelta(hours=1)


def hash_do_token(token: str) -> str:
    """SHA-256 hexadecimal do token — a forma em que o pedido é guardado e buscado.

    É o mesmo algoritmo de `sessoes.hash_do_token` e mesmo assim uma função
    separada: as duas tabelas guardam credenciais de ciclos de vida diferentes,
    e trocar a forma de guardar a sessão (que só invalida cookies, e relogar
    resolve) não pode invalidar em silêncio todos os links de recuperação já
    enviados, que ninguém consegue reemitir sozinho.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def emissoes_recentes(
    session: DbSession, usuario_id: uuid.UUID, *, agora: datetime, janela: timedelta
) -> int:
    """Quantos tokens este usuário pediu dentro da janela.

    Conta a emissão, e não o envio: um servidor SMTP que recusa a mensagem não
    pode zerar o freio, senão bastaria uma caixa postal que responde erro para o
    teto deixar de existir.
    """
    total = session.scalar(
        select(func.count())
        .select_from(TokenRecuperacao)
        .where(
            TokenRecuperacao.usuario_id == usuario_id,
            TokenRecuperacao.created_at >= agora - janela,
        )
    )
    return int(total or 0)


def emitir_token(
    session: DbSession, usuario: Usuario, *, settings: Settings, agora: datetime
) -> str | None:
    """Cria o pedido e devolve o token em claro. `None` quando o teto foi atingido. Commita.

    O `None` do teto **não** é erro para quem chama: o endpoint responde 204 do
    mesmo jeito (ver `auth/router.py`). Distinguir "enviei" de "não enviei
    porque você já pediu três vezes" diria a quem sonda que a conta existe — e
    de quebra transformaria o endpoint em metralhadora contra a caixa postal de
    quem nem pediu.

    O token do retorno é a **única** vez que ele existe em claro fora do e-mail:
    quem chama o coloca no link e o esquece.
    """
    if emissoes_recentes(session, usuario.id, agora=agora, janela=JANELA_DO_TETO) >= (
        settings.senha_reset_max_por_hora
    ):
        return None

    token = secrets.token_urlsafe(32)
    pedido = TokenRecuperacao(
        usuario_id=usuario.id,
        token_hash=hash_do_token(token),
        expires_at=agora + timedelta(minutes=settings.senha_reset_validade_minutos),
    )
    session.add(pedido)
    session.commit()
    return token


def consumir_token(session: DbSession, token: str, *, agora: datetime) -> Usuario | None:
    """Devolve o usuário do token, ou `None` quando ele não vale.

    **Não marca `used_at`** — apesar do nome, que é o do fluxo e não o do efeito:
    quem marca é `marcar_usado`, depois de a senha nova passar pela validação de
    força (ver a docstring do módulo).

    `None` cobre, sem distinguir, cinco casos: token que não existe, token
    expirado, token já usado, usuário apagado e usuário desativado. Distinguir
    qualquer um deles daria a quem tem um token velho a informação de *por que*
    ele não vale, e "já usado" diria que a conta existe.

    A linha é travada com `FOR UPDATE` até o fim da transação, e é o que torna
    "uso único" verdadeiro sob concorrência. Sem a trava, duas redefinições
    simultâneas com o mesmo token leem `used_at is None` as duas antes de
    qualquer uma marcar, e as duas passam. Com ela, a segunda transação espera
    a primeira commitar, relê a linha já marcada e devolve `None` — que é o que
    o endpoint traduz em 422.
    """
    linha = session.execute(
        select(TokenRecuperacao, Usuario)
        .join(Usuario, Usuario.id == TokenRecuperacao.usuario_id)
        .where(TokenRecuperacao.token_hash == hash_do_token(token))
        .with_for_update(of=TokenRecuperacao)
    ).first()
    if linha is None:
        return None

    pedido, usuario = linha.tuple()
    if pedido.used_at is not None or pedido.expires_at <= agora:
        return None
    # Quem saiu da operação não volta por este caminho: desativar alguém precisa
    # fechar todas as portas, e um link de recuperação emitido antes do
    # desligamento ainda estaria valendo por até `senha_reset_validade_minutos`.
    if not usuario.ativo:
        return None
    return usuario


def marcar_usado(session: DbSession, token: str, *, agora: datetime) -> None:
    """Marca o token como usado. **Não commita** — ver `auth/sessoes.revogar_todas`.

    O commit é de quem chama porque a marcação entra na mesma transação da senha
    nova e da revogação das sessões: as três coisas entram juntas ou não entram.
    Um commit aqui abriria a janela em que o token já não vale e a senha ainda é
    a antiga — e a pessoa ficaria sem os dois caminhos.
    """
    pedido = session.scalars(
        select(TokenRecuperacao).where(TokenRecuperacao.token_hash == hash_do_token(token))
    ).first()
    if pedido is None or pedido.used_at is not None:
        return
    pedido.used_at = agora
