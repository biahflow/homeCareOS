"""Persistência de `regras` (CRUD) e `validacoes` (resultado de `engine.validar()`).

Este módulo nunca escreve `documentos`: quem muda status é o intake (issue #2)
e a classificação em buckets de glosa é a issue #7.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from homecareos.db.models import Regra, Validacao
from homecareos.rules.errors import CondicaoInvalidaError, RegraNaoEncontradaError
from homecareos.rules.schema import CondicaoTypeAdapter, ResultadoAvaliacao


def _validar_condicao(condicao: dict[str, Any]) -> str:
    """Valida `condicao` contra a gramática declarativa; devolve o JSON já serializado.

    Nunca deixa `pydantic.ValidationError` vazar — traduz para
    `CondicaoInvalidaError`, que o router converte em 422 sem gravar nada.
    """
    try:
        objeto = CondicaoTypeAdapter.validate_python(condicao)
    except ValidationError as exc:
        raise CondicaoInvalidaError(f"condicao inválida: {exc}") from exc
    return objeto.model_dump_json()


def listar_regras(session: Session, operadora_id: uuid.UUID | None = None) -> list[Regra]:
    stmt = select(Regra)
    if operadora_id is not None:
        stmt = stmt.where(Regra.operadora_id == operadora_id)
    stmt = stmt.order_by(Regra.created_at)
    return list(session.scalars(stmt))


def buscar_regras_ativas(session: Session, operadora_id: uuid.UUID) -> list[Regra]:
    stmt = select(Regra).where(Regra.operadora_id == operadora_id, Regra.ativo.is_(True))
    return list(session.scalars(stmt))


def criar_regra(
    session: Session,
    *,
    operadora_id: uuid.UUID,
    campo: str,
    condicao: dict[str, Any],
    acao: str,
    motivo_glosa: str,
) -> Regra:
    condicao_json = _validar_condicao(condicao)  # levanta ANTES de tocar no banco
    regra = Regra(
        operadora_id=operadora_id,
        campo=campo,
        condicao=condicao_json,
        acao=acao,
        motivo_glosa=motivo_glosa,
    )
    session.add(regra)
    session.commit()
    session.refresh(regra)
    return regra


def atualizar_regra(
    session: Session,
    regra_id: uuid.UUID,
    *,
    operadora_id: uuid.UUID,
    campo: str,
    condicao: dict[str, Any],
    acao: str,
    motivo_glosa: str,
) -> Regra:
    regra = session.get(Regra, regra_id)
    if regra is None:
        raise RegraNaoEncontradaError(f"regra {regra_id} não encontrada")
    condicao_json = _validar_condicao(condicao)  # levanta ANTES de mutar a regra
    regra.operadora_id = operadora_id
    regra.campo = campo
    regra.condicao = condicao_json
    regra.acao = acao
    regra.motivo_glosa = motivo_glosa
    session.commit()
    session.refresh(regra)
    return regra


def desativar_regra(session: Session, regra_id: uuid.UUID) -> Regra:
    regra = session.get(Regra, regra_id)
    if regra is None:
        raise RegraNaoEncontradaError(f"regra {regra_id} não encontrada")
    regra.ativo = False
    session.commit()
    session.refresh(regra)
    return regra


def registrar_validacoes(
    session: Session, documento_id: uuid.UUID, resultados: Sequence[ResultadoAvaliacao]
) -> None:
    """Grava uma linha em `validacoes` por resultado avaliado. Commita."""
    for resultado in resultados:
        session.add(
            Validacao(
                documento_id=documento_id,
                regra_id=resultado.regra_id,
                resultado=resultado.resultado,
                detalhe=resultado.detalhe,
            )
        )
    session.commit()
