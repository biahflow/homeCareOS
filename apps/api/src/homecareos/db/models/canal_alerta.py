"""Model da configuração dos canais de alerta — ADR 0006, parte 2 (issue #9).

**Uma linha por canal**, com o estado que quem opera decide. Não é uma tabela
genérica de chave-valor: o ADR descartou essa forma explicitamente, porque sem
tipo e sem validação ela vira o depósito onde configuração entra sem revisão, e
a primeira migration que precisasse mudar o formato de um valor não teria onde
se apoiar.

    canal habilitado (esta tabela)  x  credencial presente (.env)  =  canal envia

As duas perguntas continuam separadas, e só a **primeira** mora aqui. A
credencial (uazapi, SMTP) segue no `.env` nos dois mundos — são perguntas
diferentes, e as duas precisam de resposta afirmativa para um canal enviar.
Manter isso separado é o que evita alguém ligar um canal na tela e não entender
por que nada sai.

## `atualizado_*` responde "quem decidiu o estado atual", não "quem clicou por último"

Saber que o WhatsApp está desligado sem saber desde quando não ajuda numa
investigação, e é por isso que o par `(atualizado_em, atualizado_por)` vive aqui
além do histórico completo em `auditoria_canais_alerta`: a pergunta "desde
quando?" não deve exigir uma consulta paginada de auditoria.

Os três campos são **nullable**, e é o estado semeado pela migration que os
deixa assim: quando o valor veio de `ALERTAS_CANAIS` na migração de
configuração, ninguém decidiu nada ainda, e `NULL` é a única resposta honesta.
Preencher com um ator fictício ("migration", "sistema") faria a tabela mentir
sobre uma decisão que pessoa nenhuma tomou.

Pelo mesmo motivo, um `PATCH` que reenvia o valor que já está no banco **não**
os atualiza: ligar um canal já ligado não é decisão nova, é um clique.

`atualizado_por`/`atualizado_por_usuario_id` são o mesmo par de `log_conferencia`
e de `auditoria_usuarios`: rótulo legível mais identidade referencial, com o id
**nullable** porque `exigir_papel` deixa `X-API-Key` passar em qualquer papel
(`auth/dependencies.py`) e a chave mestra não tem "si mesmo" — ela sai com
rótulo `"api"` (`auth.schema.ROTULO_MAQUINA`) e sem id. Forjar um id ali
apontaria a auditoria para quem não agiu.

`canal` é `String` com `unique`, e não a chave primária: a PK é UUID como em
toda tabela do projeto, e `codigo` de `Operadora` é o precedente exato de chave
natural única sobre PK sintética. O valor é o de `alerts.schema.Canal`, que é
quem fecha a escrita — `String` e não `SAEnum` nativo pela razão de sempre
neste módulo (`alertas_enviados.canal`, `regras.acao`, `usuarios.papel`): um
canal novo não deve custar `ALTER TYPE`.

**Sem índice além da PK e do `unique`.** A tabela tem uma linha por canal — duas
hoje — e é lida inteira a cada varredura; um índice em `atualizado_por_usuario_id`
custaria escrita e não seria usado por planejador nenhum nesse tamanho. É a
diferença consciente para `auditoria_canais_alerta`, que cresce.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class ConfiguracaoCanal(Base):
    """O estado de um canal de alerta: ligado ou não, decidido por quem e quando."""

    __tablename__ = "canais_alerta"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Valor de `alerts.schema.Canal` (`whatsapp`/`email`). `unique` é o
    # invariante "uma linha por canal" declarado no banco, e não só no código.
    canal: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # A decisão de quem opera. **Não** diz que o canal envia: falta a credencial
    # (ver a docstring do módulo).
    habilitado: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Quando o estado atual foi decidido por alguém. `NULL` enquanto for o valor
    # que a migration semeou de `ALERTAS_CANAIS` — ver a docstring do módulo.
    atualizado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Rótulo legível de quem decidiu: o e-mail da pessoa, ou `"api"` para a
    # chave de integração.
    atualizado_por: Mapped[str | None] = mapped_column(String, nullable=True)
    # A identidade referencial de quem decidiu. Nullable também quando o ator
    # foi a `X-API-Key`, que não tem "si mesmo". Sem `ondelete`: usuário não é
    # apagado, é desativado (`db/models/usuario.py`).
    atualizado_por_usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
