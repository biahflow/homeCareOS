"""Seed idempotente do catálogo de regras (issue #10).

Materializa a genérica TISS por operadora, e não com `operadora_id = NULL`:
`regras.operadora_id` é `NOT NULL` e `buscar_regras_ativas(session,
operadora_id)` filtra por igualdade — tornar a coluna anulável mudaria o
contrato de `RegraOut.operadora_id` para o frontend e a semântica de toda
consulta de regra. Materializar dá, de brinde, o que a operação precisa:
desativar uma regra genérica para uma operadora só, sem afetar as outras.

Efeito colateral conhecido: operadora criada depois do seed nasce sem as
regras genéricas — basta rodar o seed de novo (é idempotente). Hoje não há
endpoint de criação de operadora, então na prática toda operadora nasce pelo
próprio seed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from homecareos.db.models import Operadora, Regra
from homecareos.db.session import get_sessionmaker
from homecareos.rules.catalogo import (
    RegraCatalogo,
    carregar_por_operadora,
    carregar_tiss,
)
from homecareos.rules.schema import CondicaoTypeAdapter


def _condicao_json(regra: RegraCatalogo) -> str:
    """Serializa `condicao` na forma canônica do schema — não `json.dumps` no dict cru.

    Mesmo caminho que `rules/repository._validar_condicao` usa na escrita via
    API: passa pelo `CondicaoTypeAdapter` e usa `model_dump_json()`, para a
    forma gravada ser exatamente a que `rules/engine.py` sabe reler com
    `CondicaoTypeAdapter.validate_json`.
    """
    objeto = CondicaoTypeAdapter.validate_python(regra.condicao)
    return objeto.model_dump_json()


def _linha(operadora_id: uuid.UUID, regra: RegraCatalogo) -> dict[str, Any]:
    return {
        "operadora_id": operadora_id,
        "codigo": regra.codigo,
        "campo": regra.campo,
        "condicao": _condicao_json(regra),
        "acao": regra.acao.value,
        "motivo_glosa": regra.motivo_glosa,
        "fonte": regra.fonte,
        "escopo": regra.escopo.value,
        "ativo": regra.ativo,
    }


def seed_regras() -> None:
    """Popula o catálogo de regras. Idempotente; nunca reativa o que foi desativado.

    `DO NOTHING` e não `DO UPDATE`: a operação ajusta regra no banco (desativa
    uma que gera ruído, afrouxa um regex) e o seed roda de novo em todo deploy.
    Um `UPDATE` desfaria em silêncio a decisão de quem opera. Mudança de
    conteúdo de regra já cadastrada é migration de dados explícita, não efeito
    colateral de seed.
    """
    # Falha alto e cedo se algum JSON for inválido: melhor não seedar nada que
    # seedar meio catálogo.
    catalogo_tiss = carregar_tiss()
    catalogo_por_operadora = carregar_por_operadora()

    session_factory = get_sessionmaker()
    with session_factory() as session:
        operadoras = session.execute(select(Operadora.id, Operadora.codigo)).all()

        linhas: list[dict[str, Any]] = []
        for operadora_id, operadora_codigo in operadoras:
            for regra in catalogo_tiss:
                linhas.append(_linha(operadora_id, regra))
            for regra in catalogo_por_operadora.get(operadora_codigo, ()):
                linhas.append(_linha(operadora_id, regra))

        if linhas:
            stmt = (
                insert(Regra)
                .values(linhas)
                .on_conflict_do_nothing(index_elements=["operadora_id", "codigo"])
            )
            session.execute(stmt)
        session.commit()
