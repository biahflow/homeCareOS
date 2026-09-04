"""Registro de auditoria administrativa de usuários (issue #30, fecha o ADR 0004).

Três funções, cada uma resolvendo uma parte do requisito duro do handoff — o
registro precisa entrar na **mesma transação** da mutação que o originou:

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
"""

from __future__ import annotations

import uuid
from typing import Any

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
