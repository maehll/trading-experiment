import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from pydantic import ValidationError

from services.agent_runtime.openrouter import (
    LLMCompletion,
    MonthlyBudgetError,
    OpenRouterAdapter,
    OpenRouterError,
)
from services.agent_runtime.service import AgentRunFailure, run_once
from services.agent_runtime.clients import ServiceCallError
from services.data_api.api import create_app as create_data_app
from services.data_api.service import get_agent_context
from services.order_gateway.api import create_app as create_gateway_app
from services.order_gateway.agent_runs import AgentRunError, record_agent_decision
from trading_core.db import make_session_factory
from trading_core.models import (
    Agent,
    AgentRun,
    AgentScenario,
    AgentVariant,
    DataQuality,
    Decision,
    Execution,
    MarketData,
    Order,
    PortfolioSnapshot,
    ResearchItem,
    RunStatus,
)
from trading_core.risk import load_risk_config
from trading_core.settings import AgentRuntimeSettings

ADMIN_KEY = "local-admin-key-that-is-at-least-32-characters"
AGENT_KEY = "local-agent-key-that-is-at-least-32-characters"
DATA_KEY = "local-data-key-that-is-at-least-32-characters"


def runtime_settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        openrouter_api_key="test-openrouter-key",
        data_api_key=DATA_KEY,
        gateway_api_key=AGENT_KEY,
    )


class FakeLLM:
    def __init__(self, content: str):
        self.content = content

    def complete(self, context, *, usd_per_eur):
        return LLMCompletion(
            content=self.content,
            provider_request_id="req-agent-a-1",
            model="openai/gpt-4o-mini",
            input_tokens=120,
            output_tokens=40,
            cost_usd=Decimal("0.001200"),
        )


class HTTPDataClient:
    def __init__(self, client):
        self.client = client

    def get_context(self, agent_id):
        response = self.client.get(
            f"/agents/{agent_id}/context", headers={"X-API-Key": DATA_KEY}
        )
        assert response.status_code == 200, response.text
        return response.json()


class HTTPGatewayClient:
    def __init__(self, client):
        self.client = client

    def _post(self, path, payload, expected_status=200):
        response = self.client.post(
            path, json=payload, headers={"X-API-Key": AGENT_KEY}
        )
        assert response.status_code == expected_status, response.text
        return response.json()

    def start_run(self, agent_id, model, trigger):
        return self._post(
            "/agent-runs",
            {"agent_id": agent_id, "model": model, "trigger": trigger},
            expected_status=201,
        )

    def finish_run(self, run_id, payload):
        return self._post(f"/agent-runs/{run_id}/finish", payload)

    def record_decision(self, payload):
        return self._post("/decisions", payload, expected_status=201)

    def submit_order(self, payload):
        return self._post("/orders", payload)


def add_market_data(session, *, symbol, currency, price, metadata):
    row = MarketData(
        symbol=symbol,
        source="test-market-feed",
        currency=currency,
        price=Decimal(price),
        as_of=datetime.now(UTC),
        quality=DataQuality.ok,
        meta=metadata,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_data_context_selects_freshest_instruments_across_full_universe(session):
    session.add(
        Agent(
            id="agent-a",
            name="Agent A",
            scenario=AgentScenario.free,
            variant=AgentVariant.adaptive,
            config={},
        )
    )
    now = datetime.now(UTC)
    session.add_all(
        [
            MarketData(
                symbol=f"AAA{index:04d}",
                source="test-market-feed",
                currency="EUR",
                price=Decimal("10"),
                as_of=now - timedelta(seconds=1, microseconds=index),
                quality=DataQuality.ok,
                meta={"instrument_type": "stock"},
            )
            for index in range(2001)
        ]
    )
    session.add(
        MarketData(
            symbol="ZZZ",
            source="test-market-feed",
            currency="EUR",
            price=Decimal("10"),
            as_of=now - timedelta(milliseconds=1),
            quality=DataQuality.ok,
            meta={"instrument_type": "stock"},
        )
    )
    session.commit()

    context = get_agent_context(session, "agent-a", max_age_seconds=120)

    assert len(context["instruments"]) == 200
    assert context["universe_truncated"] is True
    assert "ZZZ" in {item["symbol"] for item in context["instruments"]}


@contextmanager
def setup_market_and_applications(test_engine, session, risk_policy):
    agent = Agent(
        id="agent-a",
        name="Agent A",
        scenario=AgentScenario.free,
        variant=AgentVariant.adaptive,
        config={},
    )
    session.add(agent)
    quote = add_market_data(
        session,
        symbol="TEST",
        currency="EUR",
        price="100",
        metadata={"instrument_type": "stock", "name": "Test AG"},
    )
    fx = add_market_data(
        session,
        symbol="EURUSD",
        currency="USD",
        price="1.08",
        metadata={"instrument_type": "fx", "rate_convention": "USD_per_EUR"},
    )
    research = ResearchItem(
        source="test-news",
        title="Company update",
        summary="A short verified test item.",
        content="Test context.",
        fetched_at=datetime.now(UTC),
    )
    session.add(research)
    session.commit()

    factory = make_session_factory(test_engine)
    gateway_app = create_gateway_app(
        factory, risk_policy, ADMIN_KEY, agent_api_key=AGENT_KEY
    )
    data_app = create_data_app(factory, DATA_KEY)
    with TestClient(gateway_app) as gateway_http, TestClient(data_app) as data_http:
        initialized = gateway_http.post(
            "/admin/agents/agent-a/initialize",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert initialized.status_code == 200, initialized.text
        yield (
            HTTPDataClient(data_http),
            HTTPGatewayClient(gateway_http),
            quote,
            fx,
            research,
        )


@pytest.fixture
def risk_policy():
    return load_risk_config()


@pytest.mark.parametrize(
    ("action", "symbol", "quantity"),
    [
        ("BUY", "TEST", "1"),
        ("SELL", "TEST", "1"),
        ("HOLD", "TEST", None),
        ("NO_TRADE", None, None),
    ],
)
def test_end_to_end_decision_and_order_boundary(
    test_engine, session, risk_policy, action, symbol, quantity
):
    with setup_market_and_applications(
        test_engine, session, risk_policy
    ) as (data_client, gateway, quote, _fx, research):
        source_ids = [
            f"market_data:{quote.id}",
            f"research_item:{research.id}",
        ]
        if action == "SELL":
            buy_proposal = json.dumps(
                {
                    "action": "BUY",
                    "symbol": "TEST",
                    "quantity": "1",
                    "rationale": "Voraussetzung für den Verkaufstest.",
                    "source_ids": source_ids,
                    "investment_horizon_days": 30,
                    "confidence": 0.8,
                }
            )
            run_once(
                runtime_settings(),
                data_client,
                gateway,
                FakeLLM(buy_proposal),
            )
        proposal = json.dumps(
            {
                "action": action,
                "symbol": symbol,
                "quantity": quantity,
                "rationale": "Testentscheidung anhand aktueller Daten.",
                "source_ids": source_ids,
                "investment_horizon_days": 30,
                "confidence": 0.8,
            }
        )
        result = run_once(runtime_settings(), data_client, gateway, FakeLLM(proposal))

        assert result["action"] == action
        if action in {"BUY", "SELL"}:
            assert result["order"]["status"] == "filled"
            expected_orders = 2 if action == "SELL" else 1
            assert session.scalar(select(func.count(Order.id))) == expected_orders
            assert session.scalar(select(func.count(Execution.id))) == expected_orders
            assert session.scalar(select(func.count(PortfolioSnapshot.id))) >= (
                expected_orders + 1
            )
        else:
            assert result["order"] is None
            assert session.scalar(select(func.count(Order.id))) == 0
            assert session.scalar(select(func.count(Execution.id))) == 0

        run = session.scalar(
            select(AgentRun).order_by(AgentRun.started_at.desc()).limit(1)
        )
        decision = session.scalar(
            select(Decision).order_by(Decision.created_at.desc()).limit(1)
        )
        assert run.status is RunStatus.succeeded
        assert run.provider_request_id == "req-agent-a-1"
        assert run.input_tokens == 120
        assert run.cost_usd == Decimal("0.001200")
        assert decision.hypotheses["action"] == action
        assert f"market_data:{quote.id}" in decision.hypotheses["source_ids"]
        assert decision.market_context["data_context"]["captured_at"]


def test_invalid_llm_output_is_logged_without_order(
    test_engine, session, risk_policy
):
    with setup_market_and_applications(
        test_engine, session, risk_policy
    ) as (data_client, gateway, *_):
        with pytest.raises(AgentRunFailure):
            run_once(
                runtime_settings(),
                data_client,
                gateway,
                FakeLLM('{"action":"BUY"}'),
            )

        assert session.scalar(select(func.count(Order.id))) == 0
        assert session.scalar(select(func.count(Decision.id))) == 0
        run = session.scalar(select(AgentRun))
        assert run.status is RunStatus.failed
        assert run.cost_usd == Decimal("0.001200")


def test_missing_budget_fx_fails_without_order(test_engine, session, risk_policy):
    with setup_market_and_applications(
        test_engine, session, risk_policy
    ) as (data_client, gateway, *_):
        session.query(MarketData).filter(MarketData.symbol == "EURUSD").delete()
        session.commit()

        with pytest.raises(AgentRunFailure, match="EURUSD"):
            run_once(
                runtime_settings(),
                data_client,
                gateway,
                FakeLLM('{"action":"NO_TRADE"}'),
            )

        assert session.scalar(select(func.count(Order.id))) == 0
        assert session.scalar(select(func.count(Decision.id))) == 0
        assert session.scalar(select(AgentRun)).status is RunStatus.failed


def test_failed_provider_request_has_unknown_usage_not_zero(
    test_engine, session, risk_policy
):
    with setup_market_and_applications(
        test_engine, session, risk_policy
    ) as (data_client, gateway, *_):
        class FailedLLM:
            def complete(self, context, *, usd_per_eur):
                raise OpenRouterError("provider unavailable")

        with pytest.raises(AgentRunFailure):
            run_once(runtime_settings(), data_client, gateway, FailedLLM())

        run = session.scalar(select(AgentRun))
        assert run.status is RunStatus.failed
        assert run.cost_usd is None
        assert run.input_tokens is None
        assert session.scalar(select(func.count(Order.id))) == 0


def test_data_api_failure_is_logged_without_order(test_engine, session, risk_policy):
    with setup_market_and_applications(
        test_engine, session, risk_policy
    ) as (_data_client, gateway, *_):
        class FailedDataClient:
            def get_context(self, agent_id):
                raise ServiceCallError("Data API unavailable")

        with pytest.raises(AgentRunFailure):
            run_once(
                runtime_settings(),
                FailedDataClient(),
                gateway,
                FakeLLM("{}"),
            )

        run = session.scalar(select(AgentRun))
        assert run.status is RunStatus.failed
        assert run.cost_usd is None
        assert session.scalar(select(func.count(Order.id))) == 0


def test_empty_market_universe_records_no_trade_without_llm(
    test_engine, session, risk_policy
):
    with setup_market_and_applications(
        test_engine, session, risk_policy
    ) as (_data_client, gateway, *_):
        session.query(MarketData).filter(MarketData.symbol == "TEST").delete()
        session.commit()

        class UnexpectedLLM:
            def complete(self, context, *, usd_per_eur):
                raise AssertionError("LLM must not be called for an empty universe")

        result = run_once(
            runtime_settings(),
            _data_client,
            gateway,
            UnexpectedLLM(),
        )

        assert result["action"] == "NO_TRADE"
        assert result["order"] is None
        assert session.scalar(select(func.count(Order.id))) == 0
        decision = session.scalar(select(Decision))
        assert decision.hypotheses["action"] == "NO_TRADE"
        run = session.scalar(select(AgentRun))
        assert run.status is RunStatus.succeeded
        assert run.input_tokens == 0
        assert run.cost_usd == 0


def test_openrouter_requires_monthly_budget_below_eur_cap():
    calls = []

    def request_json(method, url, payload):
        calls.append((method, url, payload))
        if url.endswith("/key"):
            return {
                "data": {
                    "effective_budget": {
                        "reset_interval": "monthly",
                        "limit_usd": "18",
                        "spend_usd": "2",
                    }
                }
            }
        return {
            "id": "request-1",
            "model": "openai/gpt-4o-mini",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": '{"action":"HOLD"}'},
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cost": "0.001",
            },
        }

    result = OpenRouterAdapter(
        runtime_settings(), request_json=request_json
    ).complete({}, usd_per_eur=Decimal("1"))

    assert result.provider_request_id == "request-1"
    assert calls[1][2]["provider"]["require_parameters"] is True
    assert calls[1][2]["response_format"]["json_schema"]["strict"] is True


def test_openrouter_budget_over_cap_blocks_model_request():
    calls = []

    def request_json(method, url, payload):
        calls.append(method)
        return {
            "data": {
                "effective_budget": {
                    "reset_interval": "monthly",
                    "limit_usd": "19",
                    "spend_usd": "0",
                }
            }
        }

    with pytest.raises(MonthlyBudgetError):
        OpenRouterAdapter(
            runtime_settings(), request_json=request_json
        ).complete({}, usd_per_eur=Decimal("1"))

    assert calls == ["GET"]


def test_agent_service_urls_must_be_loopback():
    with pytest.raises(ValidationError):
        AgentRuntimeSettings(
            openrouter_api_key="test-openrouter-key",
            data_api_key=DATA_KEY,
            gateway_api_key=AGENT_KEY,
            gateway_base_url="https://example.com",
        )


def test_agent_gateway_key_cannot_use_admin_routes(test_engine, risk_policy):
    app = create_gateway_app(
        make_session_factory(test_engine),
        risk_policy,
        ADMIN_KEY,
        agent_api_key=AGENT_KEY,
    )
    with TestClient(app) as client:
        assert client.post(
            "/agent-runs",
            json={"agent_id": "agent-a", "model": "test"},
            headers={"X-API-Key": ADMIN_KEY},
        ).status_code == 401
        assert client.post(
            "/agents/agent-a/kill-switch",
            json={"reason": "not permitted"},
            headers={"X-API-Key": AGENT_KEY},
        ).status_code == 401
        assert client.post(
            "/admin/agents/agent-a/orders/match",
            headers={"X-API-Key": AGENT_KEY},
        ).status_code == 401


@pytest.mark.parametrize("foreign", [True, False])
def test_research_source_must_belong_to_agent_and_predate_context(session, foreign):
    now = datetime.now(UTC)
    session.add_all([
        Agent(id="agent-a", name="A", scenario=AgentScenario.free, variant=AgentVariant.adaptive, config={}),
        Agent(id="agent-b", name="B", scenario=AgentScenario.free, variant=AgentVariant.adaptive, config={}),
    ])
    session.flush()
    run = AgentRun(agent_id="agent-a", trigger="test", status=RunStatus.running,
                   started_at=now - timedelta(seconds=2))
    item = ResearchItem(
        agent_id="agent-b" if foreign else "agent-a", source="test",
        fetched_at=now - timedelta(seconds=1) if foreign else now + timedelta(seconds=1),
    )
    session.add_all([run, item])
    session.flush()
    run_id, item_id = run.id, item.id
    session.commit()
    with pytest.raises(AgentRunError, match="Research"):
        record_agent_decision(
            session, agent_id="agent-a", run_id=run_id, action="HOLD", symbol=None,
            quantity=None, rationale="test", investment_horizon_days=1,
            confidence=Decimal("1"), source_ids=[f"research_item:{item_id}"],
            market_context={"data_context": {
                "captured_at": now.isoformat(), "instruments": [],
                "research": [{"research_item_id": str(item_id)}],
            }},
        )


def test_buy_with_incomplete_portfolio_fails_run_without_order(test_engine, session, risk_policy):
    with setup_market_and_applications(test_engine, session, risk_policy) as (data_client, gateway, quote, *_):
        class IncompleteData:
            def get_context(self, agent_id):
                data = data_client.get_context(agent_id)
                data["portfolio"]["complete"] = False
                return data

        proposal = json.dumps({
            "action": "BUY", "symbol": "TEST", "quantity": "1", "rationale": "test",
            "source_ids": [f"market_data:{quote.id}"],
            "investment_horizon_days": 1, "confidence": 0.8,
        })
        with pytest.raises(AgentRunFailure, match="vollständiges Portfolio"):
            run_once(runtime_settings(), IncompleteData(), gateway, FakeLLM(proposal))
        assert session.scalar(select(func.count(Order.id))) == 0
        assert session.scalar(select(AgentRun)).status is RunStatus.failed
        assert session.scalar(select(Decision)).hypotheses["action"] == "BUY"


def test_known_secrets_are_not_sent_to_model_or_recorded_in_errors(test_engine, session, risk_policy):
    with setup_market_and_applications(test_engine, session, risk_policy) as (data_client, gateway, *_):
        class SecretData:
            def get_context(self, agent_id):
                data = data_client.get_context(agent_id)
                data["research"][0]["summary"] = "test-openrouter-key"
                return data

        class UnexpectedLLM:
            def complete(self, context, *, usd_per_eur):
                raise AssertionError("Geheimnis darf nicht im Prompt landen")

        with pytest.raises(AgentRunFailure, match="Prozessgeheimnis"):
            run_once(runtime_settings(), SecretData(), gateway, UnexpectedLLM())
        run = session.scalar(select(AgentRun))
        assert run.status is RunStatus.failed
        assert "test-openrouter-key" not in run.error
        assert session.scalar(select(func.count(Order.id))) == 0


def test_agent_key_cannot_finish_other_agents_run_or_read_other_context(test_engine, session, risk_policy):
    session.add(Agent(
        id="agent-b", name="B", scenario=AgentScenario.free,
        variant=AgentVariant.adaptive, config={},
    ))
    session.flush()
    run = AgentRun(agent_id="agent-b", trigger="test", status=RunStatus.running)
    session.add(run)
    session.flush()
    run_id = run.id
    session.commit()
    factory = make_session_factory(test_engine)
    with TestClient(create_gateway_app(factory, risk_policy, ADMIN_KEY, agent_api_key=AGENT_KEY)) as client:
        response = client.post(
            f"/agent-runs/{run_id}/finish",
            json={"status": "succeeded", "input_tokens": 0, "output_tokens": 0, "cost_usd": "0"},
            headers={"X-API-Key": AGENT_KEY},
        )
        assert response.status_code == 409
    with TestClient(create_data_app(factory, DATA_KEY)) as client:
        assert client.get("/agents/agent-b/context", headers={"X-API-Key": DATA_KEY}).status_code == 403
    assert session.get(AgentRun, run_id).status is RunStatus.running
