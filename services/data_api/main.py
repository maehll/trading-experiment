"""Entrypoint: python -m services.data_api.main."""

import uvicorn

from trading_core.db import make_engine, make_session_factory
from trading_core.settings import DataServiceSettings

from .api import create_app

settings = DataServiceSettings()
engine = make_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = make_session_factory(engine)
app = create_app(
    SessionFactory,
    settings.api_key.get_secret_value(),
    market_data_max_age_seconds=settings.market_data_max_age_seconds,
)


if __name__ == "__main__":
    if settings.host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Die lokale Data API darf nur an Loopback gebunden werden")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
