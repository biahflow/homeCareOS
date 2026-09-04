"""Registro de auditoria administrativa de usuários (issue #30, fecha o ADR 0004).

Três funções de escrita, cada uma resolvendo uma parte do requisito duro do
handoff — o registro precisa entrar na **mesma transação** da mutação que o
originou —, e uma quarta que apaga por idade (`limpar_auditoria_antiga`, o
expurgo por retenção da issue #39):

- `calcular_mudancas` compara o estado atual do `Usuario` com o corpo do
  `PATCH` e devolve só os campos que **de fato** mudaram de valor. Um `PATCH`
  que reenvia o valor que já está no banco não é mudança, e não deve gerar
  linha (critério de aceite 4 do handoff).
- `classificar_acao` decide o rótulo do evento a partir do diff: mudança em
  `ativo` pesa mais que mudança em `nome`/`papel`, porque é a pergunta que a
  tabela existe para responder ("quem desativou esta pessoa às pressas").
- `registrar` enfileira o evento na sessão. **Não commita.** Segue o padrão de
  `auth.sessoes.revogar_todas` e `auth.recuperacao.marcar_usado`: o commit é de
  quem chama, para o evento entrar exatamente no mesmo commit da mutação. Um
  commit aqui reproduziria a armadilha de
  `intake.repository.DocumentoRepository.registrar_log` — o registro sairia da
  transação da mutação sem o código parecer errado.
- `limpar_auditoria_antiga` apaga por idade, em lotes, para o expurgo por
  retenção (`retencao/cli.py`). Fica aqui, junto do domínio, pela mesma regra
  de `auth.protecao.limpar_tentativas_antigas` e
  `alerts.repository.limpar_alertas_antigos`: `retencao/` decide QUANDO é
  seguro apagar, cada domínio sabe COMO apagar o que é seu. É o **único**
  caminho de exclusão desta tabela — a API continua append-only, sem `DELETE`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session as DbSession

from homecareos.auth.schema import AcaoAuditoriaUsuario, UsuarioAtualizarRequest
from homecareos.db.models import AuditoriaUsuario, Usuario


def calcular_mudancas(
    usuario: Usuario, corpo: UsuarioAtualizarRequest
) -> dict[str, dict[str, Any]]:
    """O diff `{"campo": {"de": ..., "para": ...}}` entre `usuario` e `corpo`.

    Compara contra o valor **atual** do banco, e não contra "o campo veio no
    corpo": um `PATCH {"papel": "coordenador"}` em quem já é coordenador não
    muda nada de fato, mesmo que `corpo.papel` não seja `None`. Chame **antes**
    de aplicar `corpo` a `usuario` — depois da atribuição não sobra "antes" para
    comparar.
    """
    mudancas: dict[str, dict[str, Any]] = {}
    if corpo.nome is not None and corpo.nome != usuario.nome:
        mudancas["nome"] = {"de": usuario.nome, "para": corpo.nome}
    if corpo.papel is not None and corpo.papel.value != usuario.papel:
        mudancas["papel"] = {"de": usuario.papel, "para": corpo.papel.value}
    if corpo.ativo is not None and corpo.ativo != usuario.ativo:
        mudancas["ativo"] = {"de": usuario.ativo, "para": corpo.ativo}
    return mudancas


def classificar_acao(mudancas: dict[str, dict[str, Any]]) -> AcaoAuditoriaUsuario:
    """`DESATIVACAO`/`REATIVACAO` quando `ativo` mudou; `ALTERACAO` nos demais casos.

    `ativo` decide sozinho mesmo quando `nome`/`papel` mudam juntos na mesma
    chamada: é a mudança operacionalmente mais grave da tabela, e nenhum dos
    dois campos se perde — os dois continuam em `mudancas`, só o rótulo do
    evento prioriza a saída/entrada da pessoa.
    """
    mudanca_ativo = mudancas.get("ativo")
    if mudanca_ativo is not None:
        return (
            AcaoAuditoriaUsuario.REATIVACAO
            if mudanca_ativo["para"]
            else AcaoAuditoriaUsuario.DESATIVACAO
        )
    return AcaoAuditoriaUsuario.ALTERACAO


def registrar(
    session: DbSession,
    *,
    usuario: str,
    usuario_id: uuid.UUID | None,
    alvo_usuario_id: uuid.UUID,
    alvo_email: str,
    acao: AcaoAuditoriaUsuario,
    mudancas: dict[str, dict[str, Any]],
) -> None:
    """Enfileira o evento de auditoria na sessão. **Não commita** — ver a docstring do módulo."""
    session.add(
        AuditoriaUsuario(
            usuario=usuario,
            usuario_id=usuario_id,
            alvo_usuario_id=alvo_usuario_id,
            alvo_email=alvo_email,
            acao=acao.value,
            mudancas=mudancas,
        )
    )


def limpar_auditoria_antiga(
    session: DbSession, *, antes_de: datetime, lote: int = 1000, dry_run: bool = False
) -> int:
    """Apaga eventos de auditoria com `created_at < antes_de` e devolve quantos
    saíram (ou sairiam, em `dry_run`). Commita a cada lote de até `lote`
    linhas — ver `auth/protecao.limpar_tentativas_antigas` para o motivo do
    lote/commit por lote e do default de `lote`. Ver `retencao/cli.py`
    (issue #39).

    Sem exceção por linha, ao contrário de
    `auth.recuperacao.limpar_tokens_antigos`: aqui não existe evento "ainda em
    uso" que a idade não capture — a tabela é append-only e ninguém segura
    referência a uma linha dela. A proteção desta tabela é o piso de retenção
    (`retencao/janelas.MINIMO_AUDITORIA_USUARIOS`), não uma cláusula no
    `WHERE`.

    `alvo_email` é dado pessoal (ver a docstring de
    `db/models/auditoria_usuario.py`): esta é a única coisa no sistema que o
    remove de lá.
    """
    condicao = AuditoriaUsuario.created_at < antes_de
    if dry_run:
        total = session.scalar(select(func.count()).select_from(AuditoriaUsuario).where(condicao))
        return int(total or 0)

    total = 0
    while True:
        subquery = select(AuditoriaUsuario.id).where(condicao).limit(lote)
        resultado = cast(
            "CursorResult[Any]",
            session.execute(delete(AuditoriaUsuario).where(AuditoriaUsuario.id.in_(subquery))),
        )
        session.commit()
        apagadas = resultado.rowcount
        total += apagadas
        if apagadas < lote:
            break
    return total
