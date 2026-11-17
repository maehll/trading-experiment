from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from services.order_gateway.api import create_app
from services.order_gateway.domain import GatewayConflict, OrderCommand, match_open_orders, submit_order
from services.order_gateway.execution import execution_price
from services.order_gateway import domain as gateway_domain
from trading_core.db import make_session_factory
from trading_core.models import (
    DataQuality,
    Execution,
    MarketData,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PortfolioSnapshot,
    RiskEvent,
    SimulatorAccount,
)
from trading_core.risk import load_risk_config
from conftest import order_with_decision

API_KEY = "local-test-key-that-is-at-least-32-characters"

def test_execution_costs_and_limits_use_real_or_simulated_spread(risk_policy):
    quote = MarketData(symbol="TEST", price=Decimal("100"), bid=Decimal("99"),
                       ask=Decimal("101"), currency="EUR")
    order = Order(side=OrderSide.buy, order_type=OrderType.market)
    policy = risk_policy.simulator
    assert execution_price(order, quote, policy) == Decimal("101") * Decimal("1.0005")
    order.side = OrderSide.sell
    assert execution_price(order, quote, policy) == Decimal("99") * Decimal("0.9995")
    quote.bid = quote.ask = None
    order.side = OrderSide.buy
    buy = execution_price(order, quote, policy)
    order.side = OrderSide.sell
    sell = execution_price(order, quote, policy)
    assert buy > Decimal("100") > sell
    assert sell * (1 - Decimal(str(policy.fee_bps)) / 10000) < buy * (
        1 + Decimal(str(policy.fee_bps)) / 10000
    )
    order.side = OrderSide.buy
    order.order_type = OrderType.limit
    order.limit_price = Decimal("100")
    assert execution_price(order, quote, policy) is None


@pytest.fixture
def risk_policy():
    return load_risk_config()


@pytest.fixture
def gateway(test_engine, risk_policy):
    factory = make_session_factory(test_engine)
    app = create_app(factory, risk_policy, API_KEY)
    app.state.test_engine = test_engine
    with TestClient(app) as client:
        client.test_orders = {}
        yield client


def add_quote(
    session: Session,
    *,
    age: timedelta = timedelta(0),
    kind: str = "stock",
    price: Decimal = Decimal("100"),
):
    quote = MarketData(
        symbol="TEST",
        source="test-controlled-source",
        currency="EUR",
        price=price,
        bid=price - Decimal("0.10"),
        ask=price + Decimal("0.10"),
        as_of=datetime.now(UTC) - age,
        quality=DataQuality.ok,
        meta={"instrument_type": kind},
    )
    session.add(quote)
    session.commit()
    return quote


def order_body(agent_id: str, **overrides):
    request = {
        "agent_id": agent_id,
        "client_order_id": f"test-client-{agent_id}",
        "symbol": "TEST",
        "currency": "EUR",
        "side": "buy",
        "quantity": "1",
        "order_type": "market",
        "rationale": "Focused test order",
    }
    request.update(overrides)
    return request


def post_order(client, body):
    key = body["client_order_id"]
    if key not in client.test_orders:
        client.test_orders[key] = order_with_decision(client.app.state.test_engine, body)
    ids = client.test_orders[key]
    request = {**body, "agent_run_id": ids["agent_run_id"], "decision_id": ids["decision_id"]}
    return client.post("/orders", json=request, headers={"X-API-Key": API_KEY})


def test_order_requires_gateway_key(gateway):
    response = gateway.post("/orders", json=order_body("agent-a"))
    assert response.status_code == 401


def test_missing_gateway_key_fails_closed(test_engine, risk_policy):
    app = create_app(make_session_factory(test_engine), risk_policy, "")
    with TestClient(app) as client:
        response = client.post(
            "/orders",
            json=order_body("agent-a"),
            headers={"X-API-Key": "local-test-key-that-is-at-least-32-characters"},
        )
    assert response.status_code == 503


def test_market_order_is_filled_and_request_is_idempotent(
    gateway, session, agent, risk_policy
):
    add_quote(session)
    body = order_body(agent.id)

    first = post_order(gateway, body)
    second = post_order(gateway, body)

    assert first.status_code == 200
    assert first.json()["status"] == "filled"
    assert first.json()["fill_price"] is not None
    assert second.json()["duplicate"] is True
    assert session.scalar(select(func.count(Order.id))) == 1
    assert session.scalar(select(func.count(Execution.id))) == 1
    snapshots = list(
        session.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.agent_id == agent.id)
            .order_by(PortfolioSnapshot.as_of)
        )
    )
    assert len(snapshots) == 2
    assert snapshots[0].total_value_eur == risk_policy.starting_capital_eur
    fill = session.scalar(select(Execution))
    assert fill.market_data_id is not None
    assert fill.simulation_details["fee_bps"] == str(risk_policy.simulator.fee_bps)
    account = session.get(SimulatorAccount, agent.id)
    assert account.cash_eur == (
        Decimal(str(risk_policy.starting_capital_eur))
        - fill.quantity * fill.price
        - fill.fee
    )
    assert snapshots[1].total_value_eur == (
        snapshots[1].cash_eur + snapshots[1].positions_value_eur
    )
    assert snapshots[1].unrealized_pnl_eur < 0
    quote_audit = snapshots[1].metrics["market_data"][0]
    assert quote_audit["market_data_id"]
    assert quote_audit["as_of"]
    assert quote_audit["fetched_at"]
    assert quote_audit["price"] == "100.00000000"


def test_client_order_id_cannot_be_reused_for_different_request(gateway, session, agent):
    add_quote(session)
    body = order_body(agent.id)
    assert post_order(gateway, body).status_code == 200

    conflict = post_order(gateway, {**body, "quantity": "2"})
    assert conflict.status_code == 409


def test_oversized_order_is_persisted_as_rejected(gateway, session, agent):
    add_quote(session)
    response = post_order(gateway, order_body(agent.id, quantity="6"))

    assert response.status_code == 200
    assert response.json()["status"] == "risk_rejected"
    assert any("Einzelordergröße" in reason for reason in response.json()["risk_reasons"])
    assert session.scalar(select(func.count(Execution.id))) == 0
    assert session.scalar(select(func.count(RiskEvent.id))) == 1


def test_stale_price_fails_closed(gateway, session, agent):
    add_quote(session, age=timedelta(minutes=3))

    response = post_order(gateway, order_body(agent.id))

    assert response.json()["status"] == "risk_rejected"
    assert any("veraltet" in reason for reason in response.json()["risk_reasons"])


def test_unidentified_instrument_fails_closed(gateway, session, agent):
    add_quote(session, kind="leveraged_product")

    response = post_order(gateway, order_body(agent.id))

    assert response.json()["status"] == "risk_rejected"
    assert any("Instrumententyp" in reason for reason in response.json()["risk_reasons"])


def test_instrument_identifier_must_match_quote(gateway, session, agent):
    add_quote(session)

    response = post_order(
        gateway,
        order_body(agent.id, isin="DE0000000001"),
    )

    assert response.json()["status"] == "risk_rejected"
    assert any("ISIN" in reason for reason in response.json()["risk_reasons"])


def test_kill_switch_blocks_orders(gateway, session, agent):
    add_quote(session)
    open_order = post_order(
        gateway,
        order_body(
            agent.id,
            client_order_id=f"open-limit-{agent.id}",
            order_type="limit",
            limit_price="90",
        ),
    )
    activated = gateway.post(
        f"/agents/{agent.id}/kill-switch",
        json={"reason": "manual test stop"},
        headers={"X-API-Key": API_KEY},
    )
    response = post_order(gateway, order_body(agent.id))

    assert activated.status_code == 200
    persisted = session.scalar(
        select(Order).where(Order.client_order_id == f"open-limit-{agent.id}")
    )
    assert open_order.json()["status"] == "submitted"
    assert persisted.status is OrderStatus.cancelled
    assert response.json()["status"] == "risk_rejected"
    assert any("manual test stop" in reason for reason in response.json()["risk_reasons"])


def test_non_marketable_limit_can_be_cancelled(gateway, session, agent):
    add_quote(session)
    body = order_body(
        agent.id,
        client_order_id="test-limit-order",
        order_type="limit",
        limit_price="90",
    )

    response = post_order(gateway, body)
    order_id = response.json()["order_id"]
    cancelled = gateway.post(
        f"/agents/{agent.id}/orders/{order_id}/cancel",
        headers={"X-API-Key": API_KEY},
    )

    assert response.json()["status"] == "submitted"
    assert cancelled.json()["status"] == "cancelled"
    assert session.scalar(select(func.count(Execution.id))) == 0


def test_order_rejected_if_existing_position_has_stale_price(gateway, session, agent):
    add_quote(session)
    first = post_order(gateway, order_body(agent.id))
    quote = session.scalar(select(MarketData).where(MarketData.symbol == "TEST"))
    quote.quality = DataQuality.stale
    session.commit()

    second = post_order(
        gateway,
        order_body(agent.id, client_order_id="second-order"),
    )

    assert first.json()["status"] == "filled"
    assert second.json()["status"] == "risk_rejected"
    assert any("Marktdaten" in reason or "stale" in reason for reason in second.json()["risk_reasons"])


def test_open_limit_orders_reserve_position_exposure(gateway, session, agent):
    add_quote(session)

    first = post_order(
        gateway,
        order_body(
            agent.id,
            client_order_id=f"limit-a-{agent.id}",
            order_type="limit",
            limit_price="90",
            quantity="4.98",
        ),
    )
    second = post_order(
        gateway,
        order_body(
            agent.id,
            client_order_id=f"limit-b-{agent.id}",
            order_type="limit",
            limit_price="90",
            quantity="4.98",
        ),
    )
    third = post_order(
        gateway,
        order_body(
            agent.id,
            client_order_id=f"limit-c-{agent.id}",
            order_type="limit",
            limit_price="90",
            quantity="4.98",
        ),
    )

    assert first.json()["status"] == "submitted"
    assert second.json()["status"] == "submitted"
    assert third.json()["status"] == "risk_rejected"
    assert any("Positionsgröße" in reason for reason in third.json()["risk_reasons"])


def test_daily_loss_limit_activates_kill_switch(gateway, session, agent):
    add_quote(session)
    for suffix in ("a", "b"):
        response = post_order(
            gateway,
            order_body(
                agent.id,
                client_order_id=f"loss-{suffix}-{agent.id}",
                quantity="4.98",
            ),
        )
        assert response.json()["status"] == "filled"

    add_quote(session, price=Decimal("60"))
    response = post_order(
        gateway,
        order_body(agent.id, client_order_id=f"after-loss-{agent.id}"),
    )
    account = session.get(SimulatorAccount, agent.id)

    assert response.json()["status"] == "risk_rejected"
    assert any("Tagesverlustgrenze" in reason for reason in response.json()["risk_reasons"])
    assert account.kill_switch_active is True
    assert session.scalar(
        select(func.count(RiskEvent.id)).where(
            RiskEvent.event_type == "automatic_kill_switch"
        )
    ) == 1


def test_reconciliation_mismatch_blocks_order_and_latches_stop(gateway, session, agent):
    add_quote(session)
    first = post_order(gateway, order_body(agent.id))
    assert first.json()["status"] == "filled"

    account = session.get(SimulatorAccount, agent.id)
    account.cash_eur += Decimal("1")
    session.commit()
    blocked = post_order(
        gateway,
        order_body(agent.id, client_order_id=f"after-reconciliation-{agent.id}"),
    )

    assert blocked.json()["status"] == "risk_rejected"
    assert any("Reconciliation fehlgeschlagen" in reason for reason in blocked.json()["risk_reasons"])
    assert session.get(SimulatorAccount, agent.id).kill_switch_active is True


def test_parallel_orders_respect_atomic_position_limit(test_engine, risk_policy, agent, session):
    add_quote(session)
    factory = make_session_factory(test_engine)
    agent_id = agent.id
    requests = {
        name: order_with_decision(test_engine, order_body(
            agent_id, client_order_id=name, quantity="4.98"
        ))
        for name in ("parallel-a", "parallel-b", "parallel-c")
    }

    def submit(client_order_id: str):
        body = requests[client_order_id]
        command = OrderCommand(
            agent_id=agent_id,
            client_order_id=client_order_id,
            symbol="TEST",
            currency="EUR",
            side=OrderSide.buy,
            quantity=Decimal("4.98"),
            order_type=OrderType.market,
            rationale="parallel risk test",
            agent_run_id=UUID(body["agent_run_id"]),
            decision_id=UUID(body["decision_id"]),
        )
        with factory() as worker_session:
            return submit_order(worker_session, command, risk_policy).order.status

    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(submit, ["parallel-a", "parallel-b", "parallel-c"]))

    assert statuses.count(OrderStatus.filled) == 2
    assert statuses.count(OrderStatus.risk_rejected) == 1
    account = session.get(SimulatorAccount, agent_id)
    assert account is not None
    assert account.cash_eur >= Decimal("1000")


def test_open_limit_matches_new_quote_once_and_cancelled_order_stays_cancelled(
    gateway, session, agent
):
    add_quote(session)
    first = post_order(gateway, order_body(
        agent.id, client_order_id="match-one", order_type="limit", limit_price="90"
    ))
    cancelled = post_order(gateway, order_body(
        agent.id, client_order_id="match-cancel", order_type="limit", limit_price="90"
    ))
    assert first.json()["status"] == cancelled.json()["status"] == "submitted"
    assert gateway.post(
        f"/agents/{agent.id}/orders/{cancelled.json()['order_id']}/cancel",
        headers={"X-API-Key": API_KEY},
    ).json()["status"] == "cancelled"
    add_quote(session, price=Decimal("89"))
    url = f"/admin/agents/{agent.id}/orders/match"
    matched = gateway.post(url, headers={"X-API-Key": API_KEY})
    assert matched.json()["orders"][0]["status"] == "filled"
    assert gateway.post(url, headers={"X-API-Key": API_KEY}).json()["orders"] == []
    fill = session.scalar(select(Execution))
    assert fill.price <= Decimal("90")
    assert session.scalar(select(func.count(Execution.id))) == 1
    order = session.scalar(select(Order).where(Order.client_order_id == "match-one"))
    assert Decimal(order.risk_check["reserved_eur"]) == 0


def test_open_limit_rejects_stale_quotes_and_respects_kill_switch(gateway, session, agent):
    add_quote(session)
    first = post_order(gateway, order_body(
        agent.id, client_order_id="match-stale", order_type="limit", limit_price="90"
    ))
    url = f"/admin/agents/{agent.id}/orders/match"
    result = gateway.post(url, headers={"X-API-Key": API_KEY}).json()
    assert result["orders"][0]["status"] == "submitted"
    assert session.scalar(select(func.count(Execution.id))) == 0
    gateway.post(
        f"/agents/{agent.id}/kill-switch", json={"reason": "stop"},
        headers={"X-API-Key": API_KEY},
    )
    add_quote(session, price=Decimal("89"))
    assert gateway.post(url, headers={"X-API-Key": API_KEY}).json()["orders"] == []
    assert first.json()["status"] == "submitted"
    assert session.scalar(select(func.count(Execution.id))) == 0


def test_partially_filled_limit_matches_only_remaining_quantity(gateway, session, agent):
    add_quote(session)
    post_order(gateway, order_body(
        agent.id, client_order_id="partial-limit", quantity="2",
        order_type="limit", limit_price="90",
    ))
    order = session.scalar(select(Order).where(Order.client_order_id == "partial-limit"))
    account = session.get(SimulatorAccount, agent.id)
    now = datetime.now(UTC)
    account.cash_eur -= Decimal("89.089")
    order.status = OrderStatus.partially_filled
    order.updated_at = now
    session.add(Position(
        agent_id=agent.id, symbol="TEST", currency="EUR", quantity=Decimal("1"),
        avg_entry_price=Decimal("89.089"), realized_pnl=Decimal("0"),
        opened_at=now, updated_at=now,
    ))
    session.add(Execution(
        order_id=order.id, broker_execution_id="partial-fixture", executed_at=now,
        quantity=Decimal("1"), price=Decimal("89"), currency="EUR",
        fee=Decimal("0.089"), fee_currency="EUR",
    ))
    session.commit()
    add_quote(session, price=Decimal("89"))
    result = gateway.post(
        f"/admin/agents/{agent.id}/orders/match", headers={"X-API-Key": API_KEY}
    ).json()
    assert result["orders"][0]["status"] == "filled"
    assert session.scalar(select(func.sum(Execution.quantity))) == Decimal("2")
    assert session.scalar(select(Position).where(Position.closed_at.is_(None))).quantity == Decimal("2")


def test_parallel_matching_does_not_duplicate_fills(gateway, test_engine, risk_policy, session, agent):
    add_quote(session)
    agent_id = agent.id
    post_order(gateway, order_body(
        agent_id, client_order_id="parallel-match", order_type="limit", limit_price="90"
    ))
    add_quote(session, price=Decimal("89"))
    factory = make_session_factory(test_engine)

    def match(_):
        with factory() as worker:
            return match_open_orders(worker, agent_id, risk_policy)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(match, range(2)))
    assert sorted(map(len, results)) == [0, 1]
    assert session.scalar(select(func.count(Execution.id))) == 1


@pytest.mark.parametrize("field", ["avg_entry_price", "realized_pnl", "quantity"])
def test_position_ledger_inconsistencies_block_new_orders(gateway, session, agent, field):
    add_quote(session)
    assert post_order(gateway, order_body(agent.id)).json()["status"] == "filled"
    position = session.scalar(select(Position))
    setattr(position, field, getattr(position, field) + Decimal("1"))
    session.commit()
    blocked = post_order(gateway, order_body(agent.id, client_order_id=f"bad-{field}"))
    assert blocked.json()["status"] == "risk_rejected"
    assert any("Reconciliation" in reason for reason in blocked.json()["risk_reasons"])


@pytest.mark.parametrize("field", ["status", "reservation"])
def test_open_order_inconsistencies_block_new_orders(gateway, session, agent, field):
    add_quote(session)
    post_order(gateway, order_body(
        agent.id, client_order_id="open-corrupt", order_type="limit", limit_price="90"
    ))
    order = session.scalar(select(Order).where(Order.client_order_id == "open-corrupt"))
    if field == "status":
        order.status = OrderStatus.filled
    else:
        order.risk_check = {**order.risk_check, "reserved_eur": "0"}
    session.commit()
    result = post_order(gateway, order_body(agent.id, client_order_id=f"bad-open-{field}"))
    assert result.json()["status"] == "risk_rejected"
    assert any("Reconciliation" in reason for reason in result.json()["risk_reasons"])


def test_same_decision_cannot_create_two_orders(test_engine, risk_policy, session, agent):
    add_quote(session)
    body = order_with_decision(test_engine, order_body(agent.id))
    factory = make_session_factory(test_engine)

    def submit(client_id):
        command = OrderCommand(
            agent_id=body["agent_id"], client_order_id=client_id, symbol="TEST",
            currency="EUR", side=OrderSide.buy, quantity=Decimal("1"),
            order_type=OrderType.market, rationale="test",
            agent_run_id=UUID(body["agent_run_id"]), decision_id=UUID(body["decision_id"]),
        )
        with factory() as worker:
            try:
                return submit_order(worker, command, risk_policy).order.status
            except GatewayConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, ["same-decision-a", "same-decision-b"]))
    assert statuses.count(OrderStatus.filled) == 1
    assert statuses.count("conflict") == 1
    assert session.scalar(select(func.count(Execution.id))) == 1


def test_gateway_rejects_orders_without_run_and_decision(gateway, session, agent):
    add_quote(session)
    response = gateway.post("/orders", json=order_body(agent.id), headers={"X-API-Key": API_KEY})
    assert response.status_code == 422
    assert session.scalar(select(func.count(Order.id))) == 0


def test_round_trip_at_unchanged_mid_loses_spread_slippage_and_fees(gateway, session, agent):
    add_quote(session)
    assert post_order(gateway, order_body(agent.id, client_order_id="round-buy")).json()["status"] == "filled"
    assert post_order(gateway, order_body(
        agent.id, client_order_id="round-sell", side="sell"
    )).json()["status"] == "filled"
    fills = list(session.scalars(select(Execution).order_by(Execution.executed_at)))
    assert fills[0].price > Decimal("100") > fills[1].price
    assert all(fill.fee > 0 for fill in fills)
    assert session.get(SimulatorAccount, agent.id).cash_eur < Decimal("10000")
    assert session.scalar(select(Position)).realized_pnl < 0


def test_conflicting_isins_for_same_symbol_fail_closed(gateway, session, agent):
    quote = add_quote(session)
    quote.meta = {"instrument_type": "stock", "isin": "DE0000000001"}
    session.commit()
    quote = add_quote(session)
    quote.meta = {"instrument_type": "stock", "isin": "DE0000000002"}
    session.commit()
    response = post_order(gateway, order_body(agent.id))
    assert response.json()["status"] == "risk_rejected"
    assert any("Mehrdeutige" in reason for reason in response.json()["risk_reasons"])


@pytest.mark.parametrize("failure", ["risk", "simulator", "database"])
def test_internal_failures_roll_back_order_and_execution(gateway, session, agent, monkeypatch, failure):
    add_quote(session)
    if failure == "simulator":
        execute = gateway_domain.execute_order

        def fail_after_fill(*args, **kwargs):
            execute(*args, **kwargs)
            raise RuntimeError("simulator failure")

        monkeypatch.setattr(gateway_domain, "execute_order", fail_after_fill)
    else:
        def fail_risk(*args, **kwargs):
            if failure == "database":
                raise OperationalError("test", {}, RuntimeError("database failure"))
            raise RuntimeError("risk engine failure")

        monkeypatch.setattr(gateway_domain.RiskEngine, "assess", fail_risk)
    with pytest.raises((RuntimeError, OperationalError)):
        post_order(gateway, order_body(agent.id, client_order_id=f"failure-{failure}"))
    assert session.scalar(select(func.count(Order.id))) == 0
    assert session.scalar(select(func.count(Execution.id))) == 0
