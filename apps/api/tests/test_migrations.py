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

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from homecareos.alerts.schema import Canal
from homecareos.config import Settings
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

# Migration inicial, a única que cria (e portanto precisa dropar) os tipos enum.
REVISION_DOS_ENUMS = "e5c3d5af888e"

# A migration que leva a configuração dos canais para o banco (ADR 0006), e a
# única do projeto que **insere dado** e **lê configuração de ambiente**. As
# duas quebras de padrão estão justificadas na docstring dela; o que os testes
# abaixo guardam é o comportamento que as justifica.
REVISION_DOS_CANAIS = "a4d6c8b21f37"

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
    """O `downgrade()` da migration INICIAL precisa dropar os 5 enums nativos.

    A verificação é fixada em `e5c3d5af888e`, a revision que cria os tipos
    enum, e não no head do momento: o head muda a cada migration nova, e
    nenhuma delas tem obrigação de dropar tipo que não criou. Fixar na dona dos
    tipos é o que mantém o teste falando do que ele quer provar.

    Verificação estática do código da revision — não roda o downgrade de
    verdade contra o banco compartilhado (ver docstring do módulo).
    """
    script = _script_directory()
    revision = script.get_revision(REVISION_DOS_ENUMS)
    assert revision is not None

    source = inspect.getsource(revision.module.downgrade)

    for enum_name in NATIVE_ENUM_TYPES:
        assert f"'{enum_name}'" in source or f'"{enum_name}"' in source, (
            f"downgrade() não referencia o tipo enum {enum_name!r}"
        )
    assert source.count(".drop(") >= len(NATIVE_ENUM_TYPES)


def test_a_migration_de_canais_semeia_espelhando_alertas_canais(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O estado inicial da tabela de canais é o valor antigo, e não um literal.

    Fixar `whatsapp` na migration religaria o WhatsApp de quem o tivesse
    desligado e ignoraria quem já tivesse ligado o e-mail — que é exatamente a
    mudança de comportamento que ela existe para não causar. Uma tabela vazia
    seria pior ainda: nenhum canal envia, e a operação fica em silêncio a partir
    do deploy.
    """
    modulo = _script_directory().get_revision(REVISION_DOS_CANAIS).module

    for valor, esperado in [
        ("whatsapp", {"whatsapp"}),
        ("whatsapp,email", {"whatsapp", "email"}),
        (" email , whatsapp ", {"whatsapp", "email"}),
        ("", set()),
    ]:
        monkeypatch.setattr(
            modulo, "get_settings", lambda valor=valor: Settings(alertas_canais=valor)
        )
        assert modulo._canais_semeados() == esperado


def test_a_migration_de_canais_recusa_canal_desconhecido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Um typo em `ALERTAS_CANAIS` não pode virar "todos os canais desligados".

    Antes desta migration o typo já derrubava a varredura com 422; depois dela a
    variável só é lida aqui. Semear em silêncio trocaria um deploy que para —
    visível — por um deploy que sobe mudo.
    """
    modulo = _script_directory().get_revision(REVISION_DOS_CANAIS).module
    monkeypatch.setattr(modulo, "get_settings", lambda: Settings(alertas_canais="telegrama"))

    with pytest.raises(RuntimeError) as excinfo:
        modulo._canais_semeados()

    mensagem = str(excinfo.value)
    assert "telegrama" in mensagem
    for canal in Canal:
        assert canal.value in mensagem


def test_o_downgrade_de_canais_dropa_as_duas_tabelas() -> None:
    """Verificação estática, como a dos tipos enum: o banco compartilhado não
    aguenta um downgrade de verdade (ver a docstring do módulo)."""
    revision = _script_directory().get_revision(REVISION_DOS_CANAIS)
    source = inspect.getsource(revision.module.downgrade)

    assert 'op.drop_table("auditoria_canais_alerta")' in source
    assert 'op.drop_table("canais_alerta")' in source
