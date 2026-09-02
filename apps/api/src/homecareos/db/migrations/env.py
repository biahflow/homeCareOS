from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from homecareos.config import get_settings
from homecareos.db import models  # noqa: F401  (registra os models em Base.metadata)
from homecareos.db.base import Base

# Objeto de configuração do Alembic, que dá acesso aos valores do `alembic.ini`.
config = context.config

# Interpreta o arquivo de configuração para o logging do Python.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# A URL de conexão vem sempre da aplicação (`Settings`), nunca do `alembic.ini`
# — assim nenhuma credencial fica no arquivo versionado.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Metadata alvo para autogenerate. `homecareos.db.models` está vazio nesta
# fundação; outras trilhas populam os models e o autogenerate passa a
# enxergá-los automaticamente.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Roda migrations em modo 'offline' (gera SQL sem conectar ao banco)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Roda migrations em modo 'online' (conecta ao banco e aplica)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
