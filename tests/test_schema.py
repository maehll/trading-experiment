"""Schema- und Unveränderlichkeitstests für die 14 Kerntabellen."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from conftest import REPO_ROOT

from trading_core.models import (
    Decision,
    DecisionType,
    Execution,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Reward,
    Strategy,
    StrategyStatus,
)

EXPECTED_TABLES = {
    "agents",
    "agent_runs",
    "strategies",
    "decisions",
    "orders",
    "executions",
    "positions",
    "portfolio_snapshots",
    "market_data",
    "benchmark_portfolios",
    "research_items",
    "rewards",
    "learning_events",
    "risk_events",
    "experiment_configs",
    "simulator_accounts",
}


def test_all_core_tables_exist(test_engine):
    tables = set(inspect(test_engine).get_table_names())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"Fehlende Tabellen: {missing}"


def _make_decision(session, agent) -> Decision:
    d = Decision(
        agent_id=agent.id,
        decision_type=DecisionType.no_trade,
        rationale="Testentscheidung",
    )
    session.add(d)
    session.commit()
    return d


def test_decisions_are_append_only(session, agent):
    d = _make_decision(session, agent)

    d.rationale = "nachträglich geändert"
    with pytest.raises(DBAPIError, match="append-only"):
        session.commit()
    session.rollback()

    session.delete(session.get(Decision, d.id))
    with pytest.raises(DBAPIError, match="append-only"):
        session.commit()
    session.rollback()


def test_rewards_are_append_only(session, agent):
    r = Reward(
        agent_id=agent.id,
        period_start=datetime(2026, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 1, 2, tzinfo=UTC),
        reward_version="v1",
        net_return=Decimal("0.01"),
        reward_value=Decimal("1.0"),
        components={
            "formula": "test-formula",
            "formula_version": "v1",
            "agent_start": {
                "snapshot_id": "start",
                "as_of": "2026-01-01T00:00:00+00:00",
                "total_value_eur": "10000",
                "market_data": [],
            },
            "agent_end": {
                "snapshot_id": "end",
                "as_of": "2026-01-02T00:00:00+00:00",
                "total_value_eur": "10100",
                "market_data": [],
            },
            "benchmark_start": {
                "snapshot_id": "benchmark-start",
                "as_of": "2026-01-01T00:00:00+00:00",
                "total_value_eur": "10000",
                "market_data": [],
            },
            "benchmark_end": {
                "snapshot_id": "benchmark-end",
                "as_of": "2026-01-02T00:00:00+00:00",
                "total_value_eur": "10050",
                "market_data": [],
            },
        },
    )
    session.add(r)
    session.commit()

    r.reward_value = Decimal("999")
    with pytest.raises(DBAPIError, match="append-only"):
        session.commit()
    session.rollback()


def test_rewards_require_audit_inputs(session, agent):
    session.add(
        Reward(
            agent_id=agent.id,
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
            reward_version="v1",
            net_return=Decimal("0.01"),
            reward_value=Decimal("1.0"),
            components={},
        )
    )
    with pytest.raises(DBAPIError, match="ck_rewards_audit_inputs"):
        session.commit()
    session.rollback()


def test_executions_are_append_only(session, agent):
    d = _make_decision(session, agent)
    o = Order(
        client_order_id=f"test-{uuid.uuid4().hex[:12]}",
        decision_id=d.id,
        agent_id=agent.id,
        symbol="TEST",
        currency="EUR",
        side=OrderSide.buy,
        quantity=Decimal("1"),
        order_type=OrderType.market,
    )
    session.add(o)
    session.commit()

    e = Execution(
        order_id=o.id,
        broker_execution_id="exec-1",
        executed_at=datetime(2026, 1, 1, tzinfo=UTC),
        quantity=Decimal("1"),
        price=Decimal("100"),
        currency="EUR",
        fee=Decimal("1"),
    )
    session.add(e)
    session.commit()

    e.price = Decimal("50")
    with pytest.raises(DBAPIError, match="append-only"):
        session.commit()
    session.rollback()


def test_order_status_lifecycle_is_allowed(session, agent):
    d = _make_decision(session, agent)
    o = Order(
        client_order_id=f"test-{uuid.uuid4().hex[:12]}",
        decision_id=d.id,
        agent_id=agent.id,
        symbol="TEST",
        currency="EUR",
        side=OrderSide.buy,
        quantity=Decimal("1"),
        order_type=OrderType.market,
    )
    session.add(o)
    session.commit()

    o.status = OrderStatus.risk_approved
    session.commit()
    assert session.get(Order, o.id).status is OrderStatus.risk_approved


def _make_strategy(session, agent) -> Strategy:
    s = Strategy(
        agent_id=agent.id,
        version=1,
        name="momentum-v1",
        description="Teststrategie",
    )
    session.add(s)
    session.commit()
    return s


def test_strategy_core_fields_are_immutable(session, agent):
    s = _make_strategy(session, agent)

    s.description = "heimlich geändert"
    with pytest.raises(DBAPIError, match="immutable"):
        session.commit()
    session.rollback()

    session.delete(session.get(Strategy, s.id))
    with pytest.raises(DBAPIError, match="append-only|immutable"):
        session.commit()
    session.rollback()


def test_strategy_lifecycle_transition_is_allowed(session, agent):
    s = _make_strategy(session, agent)

    s.status = StrategyStatus.active
    s.activated_at = datetime(2026, 1, 1, tzinfo=UTC)
    session.commit()

    refreshed = session.get(Strategy, s.id)
    assert refreshed.status is StrategyStatus.active
    assert refreshed.activated_at is not None


def test_execution_audit_migration_preserves_existing_fills(test_engine, monkeypatch):
    database = f"trading_migrate_{uuid.uuid4().hex}"
    admin = create_engine(test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = None
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    try:
        url = test_engine.url.set(database=database)
        monkeypatch.setenv("TRADING_ALEMBIC_URL", url.render_as_string(hide_password=False))
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        command.upgrade(cfg, "c414a6b27e01")
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO agents (id, name, scenario, variant, config, is_active)
                VALUES ('legacy', 'legacy', 'free', 'adaptive', '{}', true)
            """))
            decision_id = conn.scalar(text("""
                INSERT INTO decisions (agent_id, decision_type, rationale)
                VALUES ('legacy', 'open', 'legacy') RETURNING id
            """))
            order_id = conn.scalar(text("""
                INSERT INTO orders (client_order_id, decision_id, agent_id, symbol,
                    currency, side, quantity, order_type, status, time_in_force)
                VALUES ('legacy-order', :decision_id, 'legacy', 'TEST', 'EUR',
                    'buy', 1, 'market', 'filled', 'day') RETURNING id
            """), {"decision_id": decision_id})
            execution_id = conn.scalar(text("""
                INSERT INTO executions (order_id, broker_execution_id, executed_at,
                    quantity, price, currency, fee, fee_currency)
                VALUES (:order_id, 'legacy-fill', now(), 1, 100, 'EUR', 1, 'EUR')
                RETURNING id
            """), {"order_id": order_id})
        command.upgrade(cfg, "head")
        command.check(cfg)
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT price, fee, market_data_id, simulation_details
                FROM executions WHERE id = :id
            """), {"id": execution_id}).one()
            assert row.price == Decimal("100")
            assert row.fee == Decimal("1")
            assert row.market_data_id is None and row.simulation_details is None
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}"'))
        admin.dispose()
