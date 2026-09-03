"""Model do usuário da conferência — a identidade que a auditoria passa a nomear.

Duas decisões desta tabela são de segurança, não de modelagem:

- `senha_hash` guarda **Argon2id** (`auth/senhas.py`), nunca a senha e nunca um
  digest rápido. Senha é escolhida por pessoa, tem entropia baixa e precisa de
  função lenta e com sal — o `hmac.compare_digest` de `api/auth.py` resolve o
  problema oposto (segredo de máquina, de alta entropia).
- `email` é **único no índice**, e é o índice que decide a colisão de cadastro:
  uma consulta prévia "já existe esse e-mail?" tem janela de corrida entre a
  leitura e a escrita, e dois cadastros simultâneos passariam pelos dois
  `SELECT` antes de qualquer `INSERT`. O `IntegrityError` do índice é a
  autoridade; a consulta prévia, quando existe, é só mensagem de erro melhor.

`papel` é `String` e não `SAEnum`, seguindo `regras.acao` e
`alertas_enviados.tipo`: um papel novo não deve exigir migration de tipo enum do
Postgres. O fechamento da escrita é o enum do pydantic (`auth/schema.Papel`).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class Usuario(Base):
    """Uma pessoa que opera a conferência, com o papel que define o que ela pode."""

    __tablename__ = "usuarios"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nome: Mapped[str] = mapped_column(String, nullable=False)
    # Identificador de login. Guardado sempre em minúsculas (normalizado na
    # escrita e na busca por `auth/router.py` e `auth/cli.py`): sem isso
    # `Ana@x.com` e `ana@x.com` seriam dois cadastros, e o índice único não
    # perceberia.
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    senha_hash: Mapped[str] = mapped_column(String, nullable=False)
    # Valor de `auth.schema.Papel`: conferente / coordenador / gestor.
    papel: Mapped[str] = mapped_column(String, nullable=False)
    # Desativar é o caminho de saída de alguém da operação: `resolver_sessao`
    # derruba a sessão na hora, sem esperar o token expirar. Apagar a linha não
    # serve — `log_conferencia.usuario_id` aponta para ela.
    ativo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
