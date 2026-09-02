from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from homecareos.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Engine único do processo.

    Construído sob demanda, não no import do módulo: importar `session` não
    pode abrir pool de conexão nem congelar a URL antes de um teste conseguir
    apontá-la para outro banco.
    """
    return create_engine(get_settings().database_url)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)


def get_session() -> Generator[Session, None, None]:
    """Dependency do FastAPI: uma sessão por requisição."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
