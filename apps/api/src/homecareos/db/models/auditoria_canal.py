"""Model da auditoria de mudança de canal de alerta — ADR 0006, parte 2 (issue #9).

**Quem desliga um canal silencia a operação**, e é isso que torna esta tabela
obrigatória: desligar por engano só é perceptível quando alguém repara que
parou de receber, e "por que ninguém foi avisado?" é uma pergunta que vai ser
feita. Sem histórico ela não tem resposta possível.

## Por que tabela própria, e não `auditoria_usuarios`

O ADR fechou isso e o schema confirma: `auditoria_usuarios.alvo_usuario_id` é
`NOT NULL` com FK para `usuarios`, e `auth.auditoria.calcular_mudancas` compara
campos de `Usuario`. Registrar "fulano desligou o WhatsApp" ali obrigaria a
inventar um alvo fictício, corrompendo justamente o dado que a issue #30 criou
— uma tabela cuja razão de existir é responder "quem fez o quê **em quem**".

O padrão do projeto é uma tabela de auditoria **por entidade de domínio**
(`log_conferencia` para documento, `auditoria_usuarios` para usuário), e o
canal segue a mesma regra.

## O ator é o mesmo par das outras auditorias, e o id é nullable pela mesma razão

`usuario`/`usuario_id` são rótulo legível mais identidade referencial, como em
`log_conferencia` e `auditoria_usuarios`. O id é **nullable** porque
`exigir_papel` deixa `X-API-Key` passar em qualquer papel
(`auth/dependencies.py`): uma chamada pela chave mestra pode mudar canal, e ela
sai com rótulo `"api"` (`auth.schema.ROTULO_MAQUINA`) e sem id. Forjar um id
nesse caso apontaria a auditoria para alguém que não fez nada.

## `habilitado_de`/`habilitado_para`, e não um `mudancas` JSONB

Divergência consciente da forma de `auditoria_usuarios`, que carrega o diff num
JSONB. Lá o JSONB existe porque um `PATCH` altera até três campos e a tabela
grava **uma linha por evento**; aqui o canal tem um único campo mutável, e duas
colunas booleanas dizem a mesma coisa com tipo, sem serialização e sem precisar
de `->>` para consultar. A pergunta que a tabela existe para responder — "quem
desligou o WhatsApp, e quando?" — vira `where canal = 'whatsapp' and
habilitado_para = false`.

Não há coluna `acao` pela mesma economia: `habilitado_para` já a determina
inteiramente (`true` é ligar, `false` é desligar), e um rótulo derivado que
pode divergir do dado que o deriva é uma segunda verdade esperando para
envelhecer.

## `canal` é `String` e **não** tem FK para `canais_alerta`

Deliberado, e é a mesma escolha de `alertas_enviados.canal`: o histórico não
pode ser refém do catálogo atual. Um canal retirado do sistema um dia deixaria
de ter linha em `canais_alerta`, e uma FK apagaria (ou travaria) o registro de
quem o ligou e desligou enquanto ele existia — que é exatamente o que uma
auditoria não pode perder.

Append-only, como `log_conferencia` e `auditoria_usuarios`: sem `updated_at`,
sem `DELETE` na API. A única exclusão é por idade, pelo expurgo por retenção
(`alerts.canais_repository.limpar_auditoria_canais_antiga`, chamado por
`retencao/`), com piso de **valor de auditoria** — nenhum freio de segurança lê
esta tabela. Ver `retencao/janelas.py` e a seção "Retenção e expurgo de dados"
do `apps/api/README.md`.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class AuditoriaCanal(Base):
    """Uma mudança de estado de um canal: quem, qual canal, de que valor para qual, quando."""

    __tablename__ = "auditoria_canais_alerta"
    __table_args__ = (
        # O filtro central da leitura ("o histórico deste canal") e da
        # investigação que a tabela existe para servir.
        Index("ix_auditoria_canais_alerta_canal", "canal"),
        # Filtro opcional por ator (`ator_id` do endpoint de leitura). FK não
        # indexa sozinha no Postgres — mesma razão de `ix_sessoes_usuario_id`.
        Index("ix_auditoria_canais_alerta_usuario_id", "usuario_id"),
        # A listagem ordena por `created_at` decrescente (mais recente
        # primeiro); sem índice isso é varredura sequencial a cada página
        # conforme a tabela cresce. O expurgo por idade pega carona nele, como
        # em `auditoria_usuarios`.
        Index("ix_auditoria_canais_alerta_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Rótulo legível de quem agiu: o e-mail da pessoa, ou `"api"`.
    usuario: Mapped[str] = mapped_column(String, nullable=False)
    # A identidade referencial de quem agiu. Nullable — ver a docstring do módulo.
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=True
    )
    # Valor de `alerts.schema.Canal`. Sem FK, de propósito — ver a docstring.
    canal: Mapped[str] = mapped_column(String, nullable=False)
    habilitado_de: Mapped[bool] = mapped_column(Boolean, nullable=False)
    habilitado_para: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
