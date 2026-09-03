"""Persistência da única tabela de propriedade da extração: `extracoes`.

Este módulo **não** escreve `documentos`. O intake é dono daquela tabela e a
commita antes de a extração ser disparada; se a extração pudesse alterá-la, o
dia em que ela virar fila ou serviço separado teria dois donos escrevendo o
mesmo estado a partir de transações diferentes.

O raw response do modelo não vai para o banco — só a chave de onde ele foi
guardado, em `raw_response_ref` (ver `extracao.py` e `s3_raw_store.py`).
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from homecareos.db.models import Extracao
from homecareos.extraction.schema import ExtractionResult


def registrar_extracao(
    session: Session, documento_id: uuid.UUID, resultado: ExtractionResult
) -> uuid.UUID:
    """Grava e commita uma linha em `extracoes`; devolve o id criado.

    Transação própria, sempre: nenhuma transação atravessa a fronteira entre
    intake e extração.
    """
    extracao = Extracao(
        documento_id=documento_id,
        campos_extraidos=resultado.campos.model_dump(mode="json"),
        confianca=resultado.confianca,
        confianca_por_campo=dict(resultado.confianca_por_campo),
        raw_response_ref=resultado.raw_response_key,
        modelo=resultado.modelo,
        provider=resultado.provider,
    )
    session.add(extracao)
    session.flush()
    extracao_id = extracao.id
    session.commit()
    return extracao_id
