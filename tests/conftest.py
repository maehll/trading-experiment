"""Testinfrastruktur: eigene Test-Datenbank, Migrationen via Alembic."""

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from trading_core.models import (
    Agent, AgentRun, AgentScenario, AgentVariant, Decision, DecisionType, RunStatus,
)
from trading_core.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def order_with_decision(engine, body):
    with Session(engine) as session:
        run = AgentRun(
            agent_id=body["agent_id"], trigger="test", status=RunStatus.running,
            started_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        decision = Decision(
            agent_id=body["agent_id"], agent_run_id=run.id,
            decision_type=DecisionType.open if body["side"] == "buy" else DecisionType.close,
            instrument={"symbol": body["symbol"]},
            hypotheses={"action": body["side"].upper(), "quantity": str(Decimal(body["quantity"]))},
            rationale=body["rationale"],
        )
        session.add(decision)
        session.flush()
        result = {**body, "agent_run_id": str(run.id), "decision_id": str(decision.id)}
        session.commit()
        return result


def _url(db_name: str) -> str:
    s = get_settings()
    return (
        f"postgresql+psycopg://{s.db_user}:{s.db_password}"
        f"@{s.db_host}:{s.db_port}/{db_name}"
    )


@pytest.fixture(scope="session")
def test_engine():
    test_db = f"trading_test_{uuid.uuid4().hex}"
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{test_db}"'))
    except Exception as exc:  # pragma: no cover
        admin.dispose()
        pytest.skip(f"PostgreSQL nicht erreichbar: {exc}")

    previous_url = os.environ.get("TRADING_ALEMBIC_URL")
    engine = None
    try:
        os.environ["TRADING_ALEMBIC_URL"] = _url(test_db)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        command.upgrade(cfg, "head")
        engine = create_engine(_url(test_db))
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if previous_url is None:
            os.environ.pop("TRADING_ALEMBIC_URL", None)
        else:
            os.environ["TRADING_ALEMBIC_URL"] = previous_url
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{test_db}"'))
        admin.dispose()


@pytest.fixture
def session(test_engine):
    with test_engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    simulator_accounts, risk_events, executions, orders, decisions,
                    positions, market_data, rewards, learning_events,
                    portfolio_snapshots, research_items, strategies, agent_runs,
                    experiment_configs, agents
                CASCADE
                """
            )
        )
    with Session(test_engine) as sess:
        yield sess
        sess.rollback()


@pytest.fixture
def agent(session) -> Agent:
    """Frischer Agent pro Test (eindeutige ID)."""
    a = Agent(
        id=f"agent-test-{uuid.uuid4().hex[:8]}",
        name="test-agent",
        scenario=AgentScenario.free,
        variant=AgentVariant.adaptive,
        config={},
    )
    session.add(a)
    session.commit()
    return a
