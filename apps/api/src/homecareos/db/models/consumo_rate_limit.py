"""Model de `ConsumoRateLimit` — uma linha por consumo de rota cara (ADR 0005).

**Uma linha por consumo, e a contagem é um `COUNT` sobre a janela** — mesmo
desenho de `tentativas_login`, e pela mesma razão: o volume das quatro rotas
limitadas é baixo (elas já falam com storage, provider de IA ou gateway
externo), e uma linha por evento responde "quem consumiu o quê e quando", que é
a pergunta de quem investiga um 429. Um bucket com `UPSERT` guardaria só um
número e economizaria escrita que ninguém está pagando.

**Sem FK para `usuarios`, e é deliberado.** A chave do contador é a identidade
do principal (ADR 0005), e uma delas não é uma pessoa: a integração
máquina-a-máquina autenticada por `X-API-Key` entra como `maquina:api` — ver
`limites/protecao.chave_do_principal`. Uma FK aqui obrigaria a inventar uma
linha em `usuarios` para a chave de máquina, que é exatamente o registro falso
que `log_conferencia.usuario_id` evita ao ser nullable.

**Nenhuma credencial e nenhum dado pessoal entram aqui.** `chave` guarda o *id*
do usuário, nunca o e-mail, nunca o token do cookie, nunca a chave de API: a
tabela só precisa saber "quantas vezes esta identidade consumiu este recurso na
última hora", e o id opaco basta para isso.

`recurso` é `String` e não `SAEnum`, seguindo `usuarios.papel`, `regras.acao` e
`alertas_enviados.tipo`: o ADR 0005 diz que a lista de rotas limitadas **cresce**
("se a operação começar a sofrer abuso nelas, o limite se estende"), e estender
não pode custar migration de tipo enum do Postgres. O fechamento da escrita é o
enum do Python, `limites.schema.Recurso`.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class ConsumoRateLimit(Base):
    """Um consumo de rota limitada: quem consumiu, qual recurso, e quando."""

    __tablename__ = "consumos_rate_limit"
    __table_args__ = (
        # A única consulta que existe sobre esta tabela: "quantas linhas desta
        # chave, deste recurso, desde X" — e o `min(created_at)` da mesma
        # janela, que calcula o `Retry-After`. Índice composto na ordem da
        # consulta (igualdade, igualdade, faixa). Nomeado à mão como os de
        # `tentativas_login`; e vale o lembrete de `sessoes`: no Postgres
        # índice não nasce de graça junto com nada.
        Index("ix_consumos_rate_limit_chave_recurso_created_at", "chave", "recurso", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Identidade do principal, com prefixo de namespace: `usuario:<uuid>` ou
    # `maquina:api`. O prefixo não é enfeite — é o que torna as duas famílias
    # provadamente disjuntas numa coluna só, sem FK e sem inventar um usuário
    # para a chave de máquina. Ver `limites/protecao.chave_do_principal`.
    chave: Mapped[str] = mapped_column(String, nullable=False)
    # Qual dos limites foi consumido. Valores de `limites.schema.Recurso`.
    recurso: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
