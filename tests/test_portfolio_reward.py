from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from services.order_gateway.api import create_app
from services.portfolio.service import (
    PortfolioValuationError,
    capture_snapshot,
    initialize_benchmark,
)
from services.reward_engine.service import RewardEvaluationError, evaluate_reward
from trading_core.db import make_session_factory
from trading_core.models import (
    Agent,
    AgentScenario,
    AgentVariant,
    AgentRun,
    DataQuality,
    Decision,
    DecisionType,
    Execution,
    MarketData,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
    RunStatus,
)
from trading_core.reward_config import load_reward_config
from conftest import order_with_decision

API_KEY = "local-test-key-that-is-at-least-32-characters"


@pytest.fixture
def risk_policy():
    from trading_core.risk import load_risk_config

    return load_risk_config()


def _snapshot(
    agent_id: str,
    as_of: datetime,
    *,
    total: Decimal,
    cash: Decimal,
    positions: Decimal,
    drawdown: Decimal,
    metrics: dict | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        agent_id=agent_id,
        as_of=as_of,
        total_value_eur=total,
        cash_eur=cash,
        positions_value_eur=positions,
        unrealized_pnl_eur=Decimal("0"),
        realized_pnl_eur=Decimal("0"),
        high_watermark_eur=total,
        drawdown_pct=drawdown,
        metrics=metrics or {},
    )


def _reward_inputs(
    session, agent_id: str, *, run_status=RunStatus.succeeded,
    agent_end=Decimal("11000"), benchmark_end=Decimal("10500"),
    intermediate_total=None,
):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    benchmark = Agent(
        id="benchmark-d",
        name="passive-benchmark",
        scenario=AgentScenario.benchmark,
        variant=AgentVariant.passive,
        config={},
    )
    session.add(benchmark)
    session.add_all(
        [
            _snapshot(
                agent_id,
                start,
                total=Decimal("10000"),
                cash=Decimal("10000"),
                positions=Decimal("0"),
                drawdown=Decimal("1"),
            ),
            _snapshot(
                agent_id,
                end,
                total=agent_end,
                cash=Decimal("10000"),
                positions=Decimal("1000"),
                drawdown=Decimal("2.5"),
            ),
            _snapshot(
                benchmark.id,
                start,
                total=Decimal("10000"),
                cash=Decimal("10000"),
                positions=Decimal("0"),
                drawdown=Decimal("0"),
            ),
            _snapshot(
                benchmark.id,
                end,
                total=benchmark_end,
                cash=Decimal("9500"),
                positions=Decimal("1000"),
                drawdown=Decimal("0"),
                metrics={
                    "portfolio_type": "passive_benchmark",
                    "market_data_id": "benchmark-market-data",
                    "market_data_as_of": "2026-01-01T23:59:59+00:00",
                },
            ),
        ]
    )
    if intermediate_total is not None:
        session.add(
            _snapshot(
                agent_id, start + timedelta(hours=12),
                total=intermediate_total, cash=intermediate_total,
                positions=Decimal("0"), drawdown=Decimal("0"),
            )
        )
    run = AgentRun(
        agent_id=agent_id,
        trigger="scheduled",
        status=run_status,
        started_at=start + timedelta(hours=1),
        finished_at=end - timedelta(hours=1),
        cost_usd=Decimal("10"),
    )
    decision = Decision(
        agent_id=agent_id,
        decision_type=DecisionType.open,
        rationale="reward turnover fixture",
    )
    session.add_all([run, decision])
    session.flush()
    order = Order(
        client_order_id="reward-fixture-order",
        decision_id=decision.id,
        agent_id=agent_id,
        symbol="TEST",
        currency="EUR",
        side=OrderSide.buy,
        quantity=Decimal("10"),
        order_type=OrderType.market,
        status=OrderStatus.filled,
        time_in_force="day",
    )
    session.add(order)
    session.flush()
    session.add(
        Execution(
            order_id=order.id,
            broker_execution_id="reward-fixture-fill",
            executed_at=start + timedelta(hours=2),
            quantity=Decimal("10"),
            price=Decimal("100"),
            currency="EUR",
            fee=Decimal("0"),
            fee_currency="EUR",
        )
    )
    session.add(
        MarketData(
            symbol="EURUSD",
            source="test-controlled-source",
            currency="USD",
            price=Decimal("1.25"),
            as_of=end - timedelta(seconds=1),
            fetched_at=end - timedelta(seconds=1),
            quality=DataQuality.ok,
            meta={
                "instrument_type": "fx",
                "rate_convention": "USD_per_EUR",
            },
        )
    )
    session.commit()
    return start, end


def test_snapshot_rejects_stale_position_quote(
    test_engine, risk_policy, session, agent
):
    agent_id = agent.id
    quote = MarketData(
        symbol="TEST",
        source="test-controlled-source",
        currency="EUR",
        price=Decimal("100"),
        bid=Decimal("99.9"),
        ask=Decimal("100.1"),
        as_of=datetime.now(UTC),
        quality=DataQuality.ok,
        meta={"instrument_type": "stock"},
    )
    session.add(quote)
    session.commit()
    app = create_app(make_session_factory(test_engine), risk_policy, API_KEY)
    with TestClient(app) as client:
        body = order_with_decision(test_engine, {
                "agent_id": agent_id,
                "client_order_id": f"snapshot-test-{agent_id}",
                "symbol": "TEST",
                "currency": "EUR",
                "side": "buy",
                "quantity": "1",
                "order_type": "market",
                "rationale": "snapshot valuation fixture",
            })
        response = client.post(
            "/orders",
            json=body,
            headers={"X-API-Key": API_KEY},
        )
    assert response.json()["status"] == "filled"

    quote = session.scalar(select(MarketData).where(MarketData.symbol == "TEST"))
    quote.quality = DataQuality.stale
    session.commit()

    with pytest.raises(PortfolioValuationError, match="nicht bewertbar"):
        capture_snapshot(session, agent_id, risk_policy, datetime.now(UTC))


def test_benchmark_is_initialized_and_marked_to_market(session, risk_policy):
    captured_at = datetime.now(UTC)
    entry_quote = MarketData(
        symbol="WORLD",
        source="test-controlled-source",
        currency="EUR",
        price=Decimal("100"),
        ask=Decimal("100.1"),
        as_of=captured_at - timedelta(seconds=1),
        fetched_at=captured_at - timedelta(milliseconds=500),
        quality=DataQuality.ok,
        meta={"instrument_type": "etf"},
    )
    session.add(entry_quote)
    session.commit()

    benchmark = initialize_benchmark(
        session, "WORLD", risk_policy, as_of=captured_at
    )
    benchmark_id = benchmark.agent_id
    assert benchmark.units > 0
    assert benchmark.cash_eur >= 0
    assert benchmark.cash_eur + benchmark.units * benchmark.entry_price_eur <= (
        benchmark.initial_capital_eur
    )

    next_at = datetime.now(UTC)
    session.add(
        MarketData(
            symbol="WORLD",
            source="test-controlled-source",
            currency="EUR",
            price=Decimal("110"),
            as_of=next_at - timedelta(seconds=1),
            fetched_at=next_at - timedelta(milliseconds=500),
            quality=DataQuality.ok,
            meta={"instrument_type": "etf"},
        )
    )
    session.commit()
    snapshot = capture_snapshot(session, benchmark_id, risk_policy, next_at)
    assert snapshot.total_value_eur > benchmark.initial_capital_eur
    assert snapshot.metrics["portfolio_type"] == "passive_benchmark"
    quote_audit = snapshot.metrics["market_data"][0]
    assert quote_audit["as_of"]
    assert quote_audit["fetched_at"]
    assert quote_audit["price"] == "110.00000000"

    benchmark.units += Decimal("1")
    with pytest.raises(DBAPIError, match="append-only"):
        session.commit()
    session.rollback()


def test_reward_uses_versioned_formula_and_is_idempotent(session, agent):
    agent_id = agent.id
    start, end = _reward_inputs(session, agent_id)
    config = load_reward_config()

    reward = evaluate_reward(session, agent_id, start, end, config)
    repeated = evaluate_reward(session, agent_id, start, end, config)

    assert reward.id == repeated.id
    assert reward.net_return == Decimal("0.0992")
    assert reward.benchmark_return == Decimal("0.05")
    assert reward.drawdown_increase_pct == Decimal("0")
    assert reward.turnover_pct == Decimal("10")
    assert reward.reward_value == Decimal("9.420000")
    assert Decimal(reward.components["model_cost_usd"]) == Decimal("10")
    assert Decimal(reward.components["model_cost_eur"]) == Decimal("8")
    assert reward.components["fx_market_data_id"] is not None
    assert reward.components["fx_as_of"]
    assert reward.components["fx_fetched_at"]
    assert Decimal(reward.components["fx_usd_per_eur"]) == Decimal("1.25")
    assert reward.components["formula_version"] == config.version
    assert reward.components["effective_benchmark_weight"] == 0
    assert reward.components["executions"][0]["execution_id"]
    assert reward.components["model_cost_runs"][0]["run_id"]
    assert reward.components["agent_start"]["snapshot_id"] == reward.components[
        "start_snapshot_id"
    ]
    assert reward.components["agent_start"]["as_of"].startswith("2026-01-01")
    assert reward.components["agent_end"]["total_value_eur"] == "11000.00000000"
    assert reward.components["benchmark_end"]["total_value_eur"] == "10500.00000000"
    assert reward.components["benchmark_end"]["market_data"] == [
        {
            "market_data_id": "benchmark-market-data",
            "as_of": "2026-01-01T23:59:59+00:00",
        }
    ]


def test_reward_v1_remains_reproducible_without_overwriting_v2(session, agent):
    agent_id = agent.id
    start, end = _reward_inputs(session, agent_id)
    config = load_reward_config()
    v2 = evaluate_reward(session, agent_id, start, end, config)
    v2_id = v2.id
    session.rollback()
    v1 = evaluate_reward(session, agent_id, start, end, config.model_copy(update={"version": "v1"}))
    assert v1.id != v2_id
    assert v1.reward_value == Decimal("3.670000")
    assert v1.drawdown_increase_pct == Decimal("1.5")


@pytest.mark.parametrize(
    ("agent_end", "benchmark_end", "positive"),
    [
        ("9500", "10500", False),
        ("10500", "10500", True),
        ("10200", "10500", True),
    ],
)
def test_v2_rewards_absolute_net_return(session, agent, agent_end, benchmark_end, positive):
    agent_id = agent.id
    start, end = _reward_inputs(
        session, agent_id, agent_end=Decimal(agent_end), benchmark_end=Decimal(benchmark_end)
    )
    reward = evaluate_reward(session, agent_id, start, end, load_reward_config())
    assert (reward.reward_value > 0) is positive


def test_v2_uses_maximum_period_drawdown_even_after_recovery(session, agent):
    agent_id = agent.id
    start, end = _reward_inputs(session, agent_id, intermediate_total=Decimal("9000"))
    reward = evaluate_reward(session, agent_id, start, end, load_reward_config())
    assert reward.drawdown_increase_pct == Decimal("10")
    assert reward.reward_value == Decimal("4.42")
    assert len(reward.components["drawdown_snapshots"]) == 2


def test_v2_risk_penalties_can_exceed_a_positive_return(session, agent):
    agent_id = agent.id
    start, end = _reward_inputs(
        session, agent_id, agent_end=Decimal("10200"), intermediate_total=Decimal("8000")
    )
    reward = evaluate_reward(session, agent_id, start, end, load_reward_config())
    assert reward.net_return > 0
    assert reward.reward_value < 0


def test_reward_rejects_incomplete_model_run(session, agent):
    agent_id = agent.id
    start, end = _reward_inputs(session, agent_id, run_status=RunStatus.running)

    with pytest.raises(RewardEvaluationError, match="noch nicht abgeschlossen"):
        evaluate_reward(session, agent_id, start, end, load_reward_config())
