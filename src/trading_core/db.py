"""Engine- und Session-Fabriken für PostgreSQL."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from trading_core.settings import get_settings


def make_engine(url: str | None = None, **kwargs) -> Engine:
    return create_engine(url or get_settings().database_url, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
