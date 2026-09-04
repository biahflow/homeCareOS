"""Seed idempotente das operadoras conhecidas, do catálogo de regras e dos canais.

Uso: `python -m homecareos.seed`. Rodar mais de uma vez não duplica linhas —
o `INSERT ... ON CONFLICT DO NOTHING` do Postgres garante isso sem precisar de
um round-trip extra de SELECT antes do INSERT.

## Os canais de alerta são o caso diferente, e o seed aqui é rede, não fonte

O estado inicial de `canais_alerta` é semeado pela **migration**
`a4d6c8b21f37`, e não daqui: entre o `alembic upgrade` e o `python -m
homecareos.seed` existe uma janela em que o código novo já está no ar lendo a
tabela, e uma tabela de canais vazia significa **nenhum canal envia** — a
operação em silêncio, sem erro e sem aviso. A justificativa completa está na
docstring daquela migration.

`seed_canais` existe para o canal que nascer **depois** dela: um membro novo de
`alerts.schema.Canal` sem migration de dados ficaria sem linha, e ausente conta
como desligado. Ele entra **desligado** — ligar por padrão um canal que ninguém
configurou seria mandar mensagem que ninguém pediu, o mesmo argumento que fez
`ALERTAS_CANAIS=whatsapp` ser o default.

E ele **nunca sobrescreve** decisão de quem já mexeu na tela, pela mesma regra
de `rules/seed_regras.py`: rodar o seed de novo em todo deploy não pode desfazer
ajuste feito à mão. `ON CONFLICT (canal) DO NOTHING` é o que garante isso.
"""

from sqlalchemy.dialects.postgresql import insert

from homecareos.alerts.schema import Canal
from homecareos.db.models import ConfiguracaoCanal, Operadora
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


def seed_canais() -> None:
    """Garante uma linha por canal conhecido, **desligada**, sem tocar nas existentes.

    Rede de segurança para o canal que nascer depois da migration que semeou a
    tabela — ver a docstring do módulo. Não é a fonte do estado inicial e não
    reativa nem desativa nada.
    """
    session_factory = get_sessionmaker()
    with session_factory() as session:
        for canal in Canal:
            stmt = (
                insert(ConfiguracaoCanal)
                .values(canal=canal.value, habilitado=False)
                .on_conflict_do_nothing(index_elements=[ConfiguracaoCanal.canal])
            )
            session.execute(stmt)
        session.commit()


if __name__ == "__main__":
    # Nessa ordem: as regras materializam uma linha por operadora, então
    # dependem das operadoras já existirem no banco.
    seed_operadoras()
    seed_regras()
    seed_canais()
