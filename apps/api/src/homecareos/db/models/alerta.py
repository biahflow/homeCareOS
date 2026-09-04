"""Model do log de alertas enviados — issue #9, estendido pelo ADR 0006.

DECISÃO DE PRIVACIDADE, e não detalhe de modelagem: `mensagem` guarda o texto
enviado, que inclui o nome do paciente. É desvio aparente do princípio de
`Extracao` (não replicar dado clínico no Postgres — ver a docstring de
`db/models/extracao.py`), e é consciente: auditar uma mensagem que saiu para um
telefone exige saber o que ela dizia, e o texto **já deixou o perímetro** ao ser
entregue pelo WhatsApp — a cópia no banco não é o que cria a exposição, é o que
a torna rastreável. O que não pode aparecer aqui, nunca, é o token da instância
do gateway (ver `alerts/uazapi.py`).

`tipo`, `canal` e `status` são `String`, não `SAEnum`, seguindo o que
`regras.acao` já faz: um tipo (ou canal) novo de alerta não deve exigir
migration de tipo enum do Postgres. O fechamento da escrita é o enum do pydantic
(`alerts/schema.py`).

## `destinatario` e `usuario_id` respondem perguntas diferentes (ADR 0006)

Antes do ADR 0006, `destinatario` acumulava dois papéis: identificava o
**canal** (era sempre um telefone) e identificava a **pessoa**. Com dois
canais isso quebra em silêncio — o telefone e o e-mail da mesma pessoa viram
destinatários não relacionados, e o teto de mensagens por hora **dobra sem
ninguém pedir**.

`canal` responde "por onde saiu". `usuario_id` responde "de quem é este
endereço", e é `NULL` quando o sistema não sabe: o telefone avulso de
`ALERTAS_DESTINATARIOS` não tem vínculo com pessoa nenhuma, porque não há
telefone em `usuarios`. É a chave do rate limit quando existe — ver
`alerts/repository.contar_envios_desde`.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class AlertaEnviado(Base):
    """Uma tentativa de notificação: o que foi enviado, para quem, e no que deu."""

    __tablename__ = "alertas_enviados"
    __table_args__ = (
        # Os dois índices são os das consultas da política anti-bombardeio de
        # `alerts/service.py`, que rodam a cada alerta de cada varredura.
        Index("ix_alertas_destinatario_created_at", "destinatario", "created_at"),
        Index("ix_alertas_tipo_chave_created_at", "tipo", "chave", "created_at"),
        # A chave nova do rate limit (ADR 0006). Sem ele, contar por pessoa
        # viraria seq scan numa tabela que cresce a cada varredura — e a
        # varredura roda de minuto em minuto no cron.
        Index("ix_alertas_usuario_created_at", "usuario_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Valor de `alerts.schema.TipoAlerta`.
    tipo: Mapped[str] = mapped_column(String, nullable=False)
    # Valor de `alerts.schema.Canal`: por onde a mensagem saiu. Sem default de
    # servidor de propósito — as linhas anteriores ao ADR 0006 foram
    # preenchidas com `whatsapp` pela migration (eram todas), e uma escrita
    # nova que esquecesse o canal precisa falhar, não herdar um valor.
    canal: Mapped[str] = mapped_column(String, nullable=False)
    # Identidade do **assunto** do alerta (`documento:<id>`, `volume:<data>`,
    # ...). É o que o cooldown compara para não repetir o mesmo aviso.
    chave: Mapped[str] = mapped_column(String, nullable=False)
    # O endereço que recebeu: telefone só com dígitos (normalizado por
    # `alerts.config`) no WhatsApp, e-mail no canal de e-mail.
    destinatario: Mapped[str] = mapped_column(String, nullable=False)
    # De quem é o endereço, quando o sistema sabe — ver a docstring do módulo.
    # `NULL` no telefone avulso do `.env`. Não há `ondelete`: usuário não é
    # apagado, é desativado (`db/models/usuario.py`), e apagar o histórico de
    # alerta de alguém que saiu da equipe destruiria justamente a auditoria.
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=True
    )
    # O texto exatamente como foi enviado — ver a decisão de privacidade acima.
    mensagem: Mapped[str] = mapped_column(String, nullable=False)
    # Valor de `alerts.schema.StatusAlerta`: enviado / falha / suprimido.
    status: Mapped[str] = mapped_column(String, nullable=False)
    # Erro devolvido pelo gateway ou motivo da supressão. Nulo no envio que deu
    # certo — não há o que explicar.
    detalhe: Mapped[str | None] = mapped_column(String, nullable=True)
    # Nulo nos alertas que não falam de um documento específico (volume anormal,
    # deadline de competência).
    documento_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documentos.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
