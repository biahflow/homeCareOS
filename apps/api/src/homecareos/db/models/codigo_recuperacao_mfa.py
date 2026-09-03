"""Model do código de recuperação do MFA — a saída de quem perdeu o celular.

`codigo_hash` guarda **Argon2id** (`auth/senhas.gerar_hash`), e não SHA-256 como
`sessoes.token_hash` e `tokens_recuperacao.token_hash`. A diferença é
deliberada: aqueles dois guardam tokens de 256 bits, onde não existe dicionário
que alcance; aqui são 40 bits escolhidos por gerador
(`secrets.token_hex(5)`) — entropia alta para digitar, baixa para um dump de
banco com GPU. Como o código é credencial de login completa (ele *pula* o
segundo fator), a função lenta e com sal é o que separa o dump da conta.
Argon2 já está no projeto, e o custo é pago no máximo uma vez por código.

`used_at` guarda o uso em vez de apagar a linha, pelo mesmo motivo de
`tokens_recuperacao`: reusar um código já usado precisa falhar igual a um código
que nunca existiu, e a linha preservada é o que permite auditar depois que
alguém entrou por este caminho — que é justamente o evento que merece uma
conversa ("por que você usou um código de recuperação ontem?").

A linha é apagada de verdade em dois momentos: ao desativar o MFA e ao
reconfirmar a ativação, porque aí os códigos antigos deixariam de ter dono.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class CodigoRecuperacaoMfa(Base):
    """Um código de recuperação do segundo fator: de quem é e se já foi usado."""

    __tablename__ = "codigos_recuperacao_mfa"
    __table_args__ = (
        # A FK não cria índice sozinha no Postgres, e esta é a única consulta
        # da tabela: "os códigos não usados desta pessoa"
        # (`auth/mfa.consumir_codigo_recuperacao`), no caminho de uma
        # verificação de login.
        Index("ix_codigos_recuperacao_mfa_usuario_id", "usuario_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=False
    )
    # Hash Argon2id do código — ver a docstring do módulo. Único, mas o que a
    # restrição garante é só "a mesma linha não entra duas vezes": Argon2 usa
    # sal aleatório, então dois códigos iguais produziriam hashes diferentes e
    # passariam pelo índice. Quem garante que os códigos são distintos entre si
    # é o gerador (`auth/mfa.gerar_codigos_recuperacao`).
    codigo_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Nulo enquanto o código não foi usado. Preenchido no mesmo commit que
    # completa a sessão pendente — uso único.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
