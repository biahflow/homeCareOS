"""Model da auditoria administrativa de usuários (issue #30) — o *quem* que faltava ao ADR 0004.

`log_conferencia` registra ação sobre `Documento`; esta tabela registra ação
sobre `Usuario` — criar, alterar, desativar, reativar. É a resposta a "quem deu
a este usuário o papel de coordenador, e quando?" e a "quem desativou esta
pessoa às pressas na sexta-feira?".

**Uma linha por evento**, e não uma por campo alterado. A leitura paginada
(`GET /api/usuarios/auditoria`) lista *eventos*, um por chamada de
`POST`/`PATCH`; se cada campo alterado fosse a sua própria linha, um único
`PATCH` que muda dois campos apareceria fatiado entre páginas diferentes da
paginação por offset — o mesmo problema de desempate que
`usuarios_router.listar_usuarios` já evita ordenando por `(nome, email)`. A
coluna `mudancas` carrega o diff inteiro do evento (`{"campo": {"de": ...,
"para": ...}}`), então nada se perde quando dois campos mudam na mesma
chamada.

`usuario`/`usuario_id` são o **ator** — mesmo par, mesmo propósito e mesma
razão de `usuario_id` ser nullable que `log_conferencia.usuario`/`usuario_id`
(ver `db/models/log_conferencia.py:23-34`): `exigir_papel` deixa `X-API-Key`
passar em qualquer papel (`auth/dependencies.py:115-129`), e uma chamada pela
chave mestra tem `usuario_id=None` e `usuario="api"`
(`auth/schema.ROTULO_MAQUINA`). Forjar um id nesse caso apontaria a auditoria
para alguém que não agiu.

`alvo_usuario_id`/`alvo_email` são **quem sofreu a ação** — sempre uma pessoa
real (a criação, tanto quanto o `PATCH`, sempre tem um `Usuario` de destino).
`alvo_email` é dado pessoal e é guardado apesar disso: é o que torna a linha
legível meses depois sem precisar reconstruir o nome por outro caminho, e não
amplia a exposição — quem lê este endpoint é o coordenador, o mesmo papel que
já vê o e-mail de todo mundo em `GET /api/usuarios` (ver a docstring de
`listar_usuarios`).

`acao` é `String`, não `SAEnum` nativo do Postgres — ao contrário de
`documento.status`/`pendencia.status`, que são máquina de estados com
transição validada em código (`classification.service._TRANSICOES_VALIDAS`),
o valor aqui é um rótulo de tipo de evento, sem transição nenhuma entre
linhas: o precedente mais próximo em propósito, `log_conferencia.acao`, já é
`String`. E como `usuario.papel` (`usuario.py:15-17`), o conjunto tende a
crescer conforme mais operações administrativas passem a ser auditadas — cada
valor novo não deve exigir `ALTER TYPE`. O fechamento da escrita é
`auth.schema.AcaoAuditoriaUsuario` (`enum.StrEnum`), como `Papel` já fecha
`usuario.papel`.

Nunca grava (nem aqui, nem em `mudancas`): `senha_hash`, `mfa_secret`,
`mfa_ultimo_passo`, token de definição de senha, token de sessão — a mesma
disciplina de `UsuarioOut`/`CAMPOS_PROIBIDOS`
(`tests/test_api_usuarios.py:63`).

Append-only, como `log_conferencia`: sem `updated_at`, sem `DELETE` na API. A
única exclusão é por **idade**, pelo expurgo por retenção
(`auth.auditoria.limpar_auditoria_antiga`, chamado por `retencao/`):
`RETENCAO_AUDITORIA_USUARIOS_DIAS` (5 anos por padrão), com piso de um ano
abaixo do qual o expurgo se recusa a rodar — a tabela existe para responder a
uma pergunta que aparece em investigação, muito depois do evento, e uma
retenção curta a esvaziaria. Tanto o default quanto o piso são assunção deste
time, não requisito confirmado pelo cliente/jurídico; ver "Retenção e expurgo
de dados" no `apps/api/README.md`.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from homecareos.db.base import Base


class AuditoriaUsuario(Base):
    """Um evento de administração de usuário: quem fez, em quem, o que mudou, quando."""

    __tablename__ = "auditoria_usuarios"
    __table_args__ = (
        # A FK não cria índice sozinha no Postgres, e esta é a coluna do filtro
        # obrigatório de leitura ("auditoria deste usuário").
        Index("ix_auditoria_usuarios_alvo_usuario_id", "alvo_usuario_id"),
        # Filtro opcional por ator (`ator_id` do endpoint de leitura), pela
        # mesma razão de `ix_sessoes_usuario_id`: FK não indexa sozinha.
        Index("ix_auditoria_usuarios_usuario_id", "usuario_id"),
        # A listagem ordena por `created_at` decrescente por padrão (mais
        # recente primeiro); sem índice, isso é varredura sequencial completa
        # a cada página conforme a tabela cresce.
        Index("ix_auditoria_usuarios_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Rótulo legível de quem agiu: o e-mail da pessoa, ou `"api"` para ação de
    # máquina. Mesmo par e mesmo papel de `log_conferencia.usuario`.
    usuario: Mapped[str] = mapped_column(String, nullable=False)
    # A identidade referencial de quem agiu. Nullable: chamada por `X-API-Key`
    # não tem "si mesmo" (`auth.dependencies._principal_de_maquina`), e forjar
    # um id aqui faria a auditoria apontar para alguém que não fez nada — ver a
    # docstring do módulo e `log_conferencia.usuario_id`.
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=True
    )
    # Quem sofreu a ação. Sempre preenchido: criação e alteração sempre têm um
    # `Usuario` de destino.
    alvo_usuario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=False
    )
    # Snapshot do e-mail do alvo no momento do evento — dado pessoal guardado
    # de propósito, ver a docstring do módulo.
    alvo_email: Mapped[str] = mapped_column(String, nullable=False)
    # Valor de `auth.schema.AcaoAuditoriaUsuario`: criacao / alteracao /
    # desativacao / reativacao. `String` e não `SAEnum` — ver a docstring do
    # módulo.
    acao: Mapped[str] = mapped_column(String, nullable=False)
    # O diff do evento: `{"campo": {"de": valor_anterior, "para": valor_novo}}`.
    # Na criação, `"de"` é sempre `None` (a conta ainda não existia). Nunca
    # carrega `senha_hash`, `mfa_secret`, `mfa_ultimo_passo` nem token nenhum —
    # ver a docstring do módulo.
    mudancas: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
