"""Seed idempotente das operadoras conhecidas e do catálogo de regras.

Uso: `python -m homecareos.seed`. Rodar mais de uma vez não duplica linhas —
o `INSERT ... ON CONFLICT (codigo) DO NOTHING` do Postgres garante isso sem
precisar de um round-trip extra de SELECT antes do INSERT.
"""

from sqlalchemy.dialects.postgresql import insert

from homecareos.db.models import Operadora
from homecareos.db.session import get_sessionmaker
from homecareos.rules.seed_regras import seed_regras

OPERADORAS_SEED: tuple[tuple[str, str], ...] = (
    ("Amil", "AMIL"),
    ("Unimed", "UNIMED"),
    ("Caberj", "CABERJ"),
    ("GEAP", "GEAP"),
    ("SulAmérica", "SULAMERICA"),
    ("CNU", "CNU"),
)


def seed_operadoras() -> None:
    """Insere as operadoras conhecidas, sem duplicar em execuções repetidas."""
    session_factory = get_sessionmaker()
    with session_factory() as session:
        for nome, codigo in OPERADORAS_SEED:
            stmt = (
                insert(Operadora)
                .values(nome=nome, codigo=codigo)
                .on_conflict_do_nothing(index_elements=[Operadora.codigo])
            )
            session.execute(stmt)
        session.commit()


if __name__ == "__main__":
    # Nessa ordem: as regras materializam uma linha por operadora, então
    # dependem das operadoras já existirem no banco.
    seed_operadoras()
    seed_regras()
