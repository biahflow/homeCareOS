"""Model do token de recuperação de senha — a credencial que troca uma senha.

`token_hash` e **não** o token, pela mesma razão de `db/models/sessao.py`: o
e-mail carrega um token opaco de 256 bits (`secrets.token_urlsafe(32)`) e o
banco guarda só o SHA-256 dele. Um dump de banco vazado não vira redefinição de
senha, porque do hash não se volta para o token que o link precisa apresentar —
e aqui isso pesa mais que na sessão: sessão vazada dá acesso até expirar, token
de recuperação vazado dá a conta inteira, com a senha trocada.

Sem sal e sem Argon2 aqui, de propósito e pelo mesmo motivo de lá: sal e função
lenta existem para senha (entropia baixa, escolhida por pessoa, atacável por
dicionário). Este token é aleatório, tem entropia real de sobra e não há
dicionário que o alcance.

`used_at` guarda o uso em vez de apagar a linha: a tentativa de reusar um token
já usado precisa continuar respondendo igual à de um token que nunca existiu
(ver `auth/recuperacao.consumir_token`), e a linha preservada é o que permite
auditar depois quando a senha de alguém foi trocada por este caminho.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class TokenRecuperacao(Base):
    """Um pedido de redefinição de senha: de quem é, até quando vale e se já foi usado."""

    __tablename__ = "tokens_recuperacao"
    __table_args__ = (
        # A consulta do freio de emissão: quantos tokens este usuário pediu na
        # última hora (`auth/recuperacao.emissoes_recentes`). A FK não cria
        # índice sozinha no Postgres, e sem ele o freio varre a tabela inteira
        # a cada "esqueci minha senha" — num endpoint público, que é justamente
        # o que não pode ficar caro.
        Index("ix_tokens_recuperacao_usuario_created_at", "usuario_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=False
    )
    # SHA-256 hexadecimal do token que foi por e-mail — ver a docstring do
    # módulo. Único: é por ele que a redefinição encontra o pedido.
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Nulo enquanto o token não foi usado. Preenchido pela redefinição
    # bem-sucedida, no mesmo commit da senha nova — uso único.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
