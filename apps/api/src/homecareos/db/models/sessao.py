"""Model da sessão de usuário — o estado que torna a revogação possível.

`token_hash` e **não** o token, e a razão é concreta: o cookie do navegador
carrega um token opaco de 256 bits de entropia real
(`secrets.token_urlsafe(32)`); o banco guarda só o SHA-256 dele. Um dump de
banco vazado não entrega sessão utilizável, porque do hash não se volta para o
token que o cookie precisa apresentar.

Sem sal e sem Argon2 aqui, de propósito — e é o oposto do que `usuarios.senha_hash`
faz. Sal e função lenta existem para senha: entropia baixa, escolhida por pessoa,
atacável por dicionário. O token de sessão é aleatório e tem entropia real de
sobra, não há dicionário que o alcance, e a verificação acontece a **cada
requisição** — pagar Argon2 nela seria custo por requisição sem ganho.

A sessão vive no banco (e não num JWT) porque desligar o acesso de alguém a
prontuário clínico não pode esperar um token expirar — ver o ADR 0001.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class Sessao(Base):
    """Uma sessão de login: de quem é, até quando vale e se já foi revogada."""

    __tablename__ = "sessoes"
    __table_args__ = (
        # A FK não cria índice sozinha no Postgres, e esta é a consulta de
        # "revogar todas as sessões desta pessoa" — o que se faz ao desligar
        # alguém às pressas.
        Index("ix_sessoes_usuario_id", "usuario_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=False
    )
    # SHA-256 hexadecimal do token opaco — ver a docstring do módulo. Único:
    # é por ele que toda requisição autenticada por cookie encontra a sessão.
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Nulo enquanto a sessão vale. Preenchido pelo logout e pela revogação
    # administrativa; a linha é preservada em vez de apagada para a sessão
    # revogada continuar visível em auditoria.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # `True` entre o primeiro e o segundo passo do login de quem tem MFA
    # ativado (issue #35). Enquanto for `True`, `sessoes.resolver_sessao`
    # devolve `None` e a sessão não abre rota nenhuma de `/api/*` — é o que
    # impede o segundo fator de ser uma tela que dá para pular. Só
    # `POST /api/auth/mfa/verificar` enxerga esta sessão, por
    # `sessoes.resolver_sessao_pendente`.
    mfa_pendente: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
