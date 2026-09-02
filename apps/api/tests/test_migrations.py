"""Testes de sanidade da migration inicial contra o Postgres compartilhado.

Pressupõem que `alembic upgrade head` já rodou (ver comandos de verificação
do handoff da Trilha A) — não executam `alembic downgrade`/`upgrade` aqui:
esse Postgres (`localhost:5434`) é compartilhado com outras trilhas rodando
em paralelo, e um downgrade real tomaria locks exclusivos nas tabelas que
elas podem estar usando. O ciclo upgrade → check → downgrade → upgrade
completo é validado pelos comandos manuais do handoff, não por esta suíte.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from homecareos.db.session import get_engine

API_ROOT = Path(__file__).resolve().parents[1]

# (tabela, índice) — a lista de índices obrigatórios do handoff da Trilha A.
REQUIRED_INDEXES = {
    ("documentos", "ix_documentos_status"),
    ("documentos", "ix_documentos_competencia"),
    ("documentos", "ix_documentos_paciente_id_competencia"),
    # UNIQUE constraint do Postgres é implementado como índice único — é o
    # que garante `documentos.idempotency_key` único.
    ("documentos", "documentos_idempotency_key_key"),
    ("pendencias", "ix_pendencias_status"),
    ("pendencias", "ix_pendencias_deadline"),
}

# Os 5 enums nativos do Postgres criados pela migration inicial.
NATIVE_ENUM_TYPES = (
    "documento_status",
    "documento_tipo",
    "paciente_modalidade",
    "pendencia_status",
    "validacao_resultado",
)


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "src/homecareos/db/migrations"))
    return ScriptDirectory.from_config(cfg)


def test_migration_directory_has_a_single_head() -> None:
    """Só existe uma revision inicial — sem branches/merges pendentes."""
    script = _script_directory()

    heads = script.get_heads()

    assert len(heads) == 1


def test_database_is_at_migration_head() -> None:
    script = _script_directory()
    engine = get_engine()

    with engine.connect() as connection:
        applied = connection.execute(text("select version_num from alembic_version")).scalar_one()

    assert applied == script.get_current_head()


def test_required_indexes_exist() -> None:
    engine = get_engine()

    with engine.connect() as connection:
        rows = connection.execute(
            text("select tablename, indexname from pg_indexes where schemaname = 'public'")
        ).all()

    existing = {(row.tablename, row.indexname) for row in rows}

    missing = REQUIRED_INDEXES - existing
    assert not missing, f"índices obrigatórios ausentes em pg_indexes: {missing}"


def test_all_eight_tables_exist() -> None:
    engine = get_engine()

    with engine.connect() as connection:
        rows = connection.execute(
            text("select tablename from pg_tables where schemaname = 'public'")
        ).all()

    existing = {row.tablename for row in rows}
    expected = {
        "operadoras",
        "pacientes",
        "documentos",
        "extracoes",
        "regras",
        "validacoes",
        "pendencias",
        "log_conferencia",
    }

    assert expected <= existing


def test_downgrade_drops_every_native_enum_type() -> None:
    """`downgrade()` precisa fazer DROP explícito dos 5 enums nativos.

    Verificação estática do código da revision — não roda o downgrade de
    verdade contra o banco compartilhado (ver docstring do módulo).
    """
    script = _script_directory()
    revision = script.get_revision(script.get_current_head())
    assert revision is not None

    source = inspect.getsource(revision.module.downgrade)

    for enum_name in NATIVE_ENUM_TYPES:
        assert f"'{enum_name}'" in source or f'"{enum_name}"' in source, (
            f"downgrade() não referencia o tipo enum {enum_name!r}"
        )
    assert source.count(".drop(") >= len(NATIVE_ENUM_TYPES)
