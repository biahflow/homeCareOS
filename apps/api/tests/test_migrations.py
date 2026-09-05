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
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from homecareos.alerts.schema import Canal
from homecareos.config import Settings
from homecareos.db.session import get_engine
from tests.conftest import TEST_MFA_SECRET_KEY, TEST_MFA_SECRET_KEY_ANTIGA

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


# --- f2b9d6e04a17: cifra do segredo TOTP em repouso (ADR 0008) ---------------

# A migration que reescreve `usuarios.mfa_secret` como token Fernet. Como as
# outras verificações deste módulo, ela é exercitada SEM rodar o
# upgrade/downgrade de verdade: o Postgres de `localhost:5434` é compartilhado, e
# esta migration escreve em `usuarios`, que as outras suítes usam.
#
# O que se prova aqui é a decisão que a migration toma antes de escrever — ter
# ou não ter chave — e a lógica de cifra dela, que é o ponto onde um erro
# deixaria dado ilegível para sempre.
REVISION_DA_CIFRA = "f2b9d6e04a17"

SEGREDO_TOTP = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def _modulo_da_cifra() -> ModuleType:
    return _script_directory().get_revision(REVISION_DA_CIFRA).module


def test_a_migration_da_cifra_sucede_a_cabeca_anterior() -> None:
    """A cadeia não pode ganhar um segundo head sem ninguém perceber."""
    revision = _script_directory().get_revision(REVISION_DA_CIFRA)

    assert revision.down_revision == REVISION_DOS_CANAIS


def test_a_migration_da_cifra_le_a_chave_da_configuracao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Settings`, e não `os.environ`, pela mesma razão de `a4d6c8b21f37`: fora
    do Compose a variável costuma viver no `.env`, que `os.environ` não enxerga.
    """
    modulo = _modulo_da_cifra()

    monkeypatch.setattr(modulo, "get_settings", lambda: Settings(mfa_secret_keys=""))
    assert modulo._cifrador() is None

    monkeypatch.setattr(
        modulo, "get_settings", lambda: Settings(mfa_secret_keys=TEST_MFA_SECRET_KEY)
    )
    assert modulo._cifrador() is not None


def test_a_migration_da_cifra_recusa_chave_malformada_sem_ecoa_la(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Como `a4d6c8b21f37` faz com um canal desconhecido: um typo não vira
    silêncio. E a chave nunca aparece na mensagem — saída de migration que falha
    é o que se cola inteira num ticket."""
    modulo = _modulo_da_cifra()
    quebrada = "isto-nao-e-chave-fernet"
    monkeypatch.setattr(modulo, "get_settings", lambda: Settings(mfa_secret_keys=quebrada))

    with pytest.raises(RuntimeError) as excinfo:
        modulo._cifrador()

    mensagem = str(excinfo.value)
    assert "MFA_SECRET_KEYS" in mensagem
    assert quebrada not in mensagem


def test_a_cifra_da_migration_faz_ida_e_volta_e_aceita_rotacao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O `downgrade()` precisa decifrar o que o `upgrade()` cifrou — inclusive
    depois de uma rotação de chave.

    É o teste que impede o pior desfecho possível desta migration: um rollback
    que deixasse token Fernet na coluna devolveria o banco ao esquema antigo com
    conteúdo ilegível, o código anterior o trataria como segredo base32 válido, e
    nenhum código TOTP jamais bateria — para todo mundo, sem erro em log nenhum.
    """
    modulo = _modulo_da_cifra()

    monkeypatch.setattr(
        modulo, "get_settings", lambda: Settings(mfa_secret_keys=TEST_MFA_SECRET_KEY_ANTIGA)
    )
    cifrado = modulo._cifrador().encrypt(SEGREDO_TOTP.encode()).decode()
    assert cifrado.startswith(modulo.PREFIXO_FERNET)
    assert SEGREDO_TOTP not in cifrado

    # Rotação: chave nova na frente, antiga ainda na lista.
    monkeypatch.setattr(
        modulo,
        "get_settings",
        lambda: Settings(mfa_secret_keys=f"{TEST_MFA_SECRET_KEY},{TEST_MFA_SECRET_KEY_ANTIGA}"),
    )
    assert modulo._cifrador().decrypt(cifrado.encode()).decode() == SEGREDO_TOTP


def test_os_dois_sentidos_da_cifra_param_sem_chave_quando_ha_o_que_converter() -> None:
    """Verificação estática, como a dos tipos enum: o banco compartilhado não
    aguenta rodar esta migration de verdade (ver a docstring do módulo).

    O que ela guarda é a decisão, não a implementação: os dois sentidos precisam
    LEVANTAR quando falta chave e existe linha para converter. Pular em silêncio
    deixaria a coluna metade cifrada e metade não — sem ninguém saber quais —, e
    cada pessoa descobriria pelo login que parou de funcionar.
    """
    revision = _script_directory().get_revision(REVISION_DA_CIFRA)

    for sentido in (revision.module.upgrade, revision.module.downgrade):
        fonte = inspect.getsource(sentido)
        assert "if cifrador is None:" in fonte, f"{sentido.__name__} não checa a ausência de chave"
        assert "raise RuntimeError(" in fonte, f"{sentido.__name__} não para sem chave"
        # E o caso "não há o que converter" continua rodando sem chave: é o do
        # CI e o de todo ambiente que ainda não usa o segundo fator.
        assert "if not segredos:" in fonte


def test_o_downgrade_da_cifra_grava_o_segredo_decifrado() -> None:
    """Um `downgrade()` que só dropasse coluna, ou que não escrevesse nada,
    passaria em todo teste estático acima e ainda assim deixaria o banco
    ilegível. Esta asserção é sobre o que ele ESCREVE."""
    fonte = inspect.getsource(_script_directory().get_revision(REVISION_DA_CIFRA).module.downgrade)

    assert "decrypt(" in fonte
    assert "_gravar(" in fonte
