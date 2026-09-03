"""Model de `TentativaLogin` — registro bruto de cada tentativa de `POST /api/auth/login`.

**Sem FK para `usuarios`, e é deliberado.** A tabela registra a *string de
e-mail tentada*, não um usuário: metade das linhas úteis é de e-mail que não
existe, e uma FK tornaria impossível gravá-las — que é justamente o registro
que fecha a enumeração de usuário (issue #30/#33, ver `auth/protecao.py`).
Contar só para e-mail cadastrado reabriria o mesmo vazamento por outro
caminho.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class TentativaLogin(Base):
    """Uma tentativa de login: qual e-mail, de qual IP, e se deu certo."""

    __tablename__ = "tentativas_login"
    __table_args__ = (
        # As duas consultas de janela de `auth/protecao.py`: falhas por e-mail
        # tentado (trava de conta e atraso) e falhas por IP (trava de IP).
        Index("ix_tentativas_login_email_created_at", "email_tentado", "created_at"),
        Index("ix_tentativas_login_ip_created_at", "ip", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Normalizado em minúsculas por `auth.router.normalizar_email`, exista ou
    # não a conta — ver a docstring da classe.
    email_tentado: Mapped[str] = mapped_column(String, nullable=False)
    # Origem resolvida por `auth/protecao.ip_do_request`; ver lá a decisão
    # sobre confiar (ou não) em `X-Forwarded-For`.
    ip: Mapped[str] = mapped_column(String, nullable=False)
    sucesso: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
