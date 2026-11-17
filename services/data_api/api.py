"""Authenticated, read-only data API for the isolated agent runtime."""

from __future__ import annotations

import secrets
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy.orm import Session, sessionmaker

from .service import DataUnavailable, get_agent_context


def create_app(
    session_factory: sessionmaker[Session],
    api_key: str,
    *,
    market_data_max_age_seconds: int = 120,
) -> FastAPI:
    app = FastAPI(
        title="Trading Experiment Data API",
        version="0.1.0",
        description="Read-only curated data; no order, risk, or valuation-write routes.",
    )

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if not api_key or len(api_key) < 32:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Data API nicht konfiguriert",
            )
        if x_api_key is None or not secrets.compare_digest(x_api_key, api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Ungültiger oder fehlender Data-API-Schlüssel",
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "access": "read_only"}

    @app.get("/agents/{agent_id}/context")
    def agent_context(
        agent_id: str,
        _: None = Depends(require_api_key),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        if agent_id != "agent-a":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Data-API-Schlüssel ist ausschließlich für Agent A gültig",
            )
        try:
            return get_agent_context(
                session,
                agent_id,
                max_age_seconds=market_data_max_age_seconds,
            )
        except DataUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc

    return app
