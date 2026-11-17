"""Entrypoint: python -m services.order_gateway.main"""

import uvicorn

from trading_core.db import make_engine, make_session_factory
from trading_core.risk import load_risk_config
from trading_core.settings import get_settings

from .api import create_app

settings = get_settings()
engine = make_engine(pool_pre_ping=True)
SessionFactory = make_session_factory(engine)
risk_policy = load_risk_config()
app = create_app(
    SessionFactory,
    risk_policy,
    settings.gateway_api_key,
    settings.agent_gateway_api_key,
)


if __name__ == "__main__":
    if settings.gateway_host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Das lokale Order Gateway darf nur an Loopback gebunden werden")
    uvicorn.run(
        app,
        host=settings.gateway_host,
        port=settings.gateway_port,
        log_level="info",
    )
