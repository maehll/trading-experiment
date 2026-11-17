"""Narrowly scoped persistence for agent run and no-order decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_core.models import (
    Agent,
    AgentRun,
    AgentScenario,
    DataQuality,
    Decision,
    DecisionType,
    MarketData,
    ResearchItem,
    RunStatus,
)


class AgentRunError(RuntimeError):
    """A run or decision request is invalid for the gateway."""


def verify_source_ids(
    session: Session, source_ids: list[str], agent_id: str, run: AgentRun, market_context: dict
) -> None:
    market_ids: list[UUID] = []
    research_ids: list[UUID] = []
    try:
        for source_id in source_ids:
            kind, identifier = source_id.split(":", maxsplit=1)
            if kind == "market_data":
                market_ids.append(UUID(identifier))
            elif kind == "research_item":
                research_ids.append(UUID(identifier))
            else:
                raise ValueError
    except ValueError as exc:
        raise AgentRunError("Entscheidung enthält eine ungültige Quellenreferenz") from exc

    if not source_ids:
        return
    context = market_context.get("data_context")
    if not isinstance(context, dict):
        raise AgentRunError("Datenkontext der Entscheidung fehlt")
    try:
        captured_at = datetime.fromisoformat(context["captured_at"])
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError
        context_ids = {
            f"market_data:{item['market_data_id']}" for item in context["instruments"]
        } | {
            f"research_item:{item['research_item_id']}" for item in context["research"]
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentRunError("Datenkontext enthält ungültige Quellen oder Zeitpunkte") from exc
    if not run.started_at <= captured_at <= datetime.now(UTC) or not set(source_ids) <= context_ids:
        raise AgentRunError("Quellen stammen nicht aus dem zeitgerechten Kontext dieses Runs")
    quotes = list(session.scalars(select(MarketData).where(MarketData.id.in_(market_ids))))
    if {quote.id for quote in quotes} != set(market_ids) or any(
        quote.as_of > captured_at or quote.fetched_at > captured_at
        or quote.quality is not DataQuality.ok
        for quote in quotes
    ):
        raise AgentRunError("Entscheidung referenziert unbekannte oder spätere Marktdaten")
    items = list(session.scalars(select(ResearchItem).where(ResearchItem.id.in_(research_ids))))
    if {item.id for item in items} != set(research_ids) or any(
        item.agent_id not in (None, agent_id)
        or item.fetched_at > captured_at
        or (item.published_at is not None and item.published_at > captured_at)
        for item in items
    ):
        raise AgentRunError("Research-Daten sind fremd, unbekannt oder nicht zeitgerecht")


def start_agent_run(
    session: Session,
    agent_id: str,
    model: str,
    trigger: str = "scheduled",
) -> AgentRun:
    if agent_id != "agent-a":
        raise AgentRunError("Iteration 4 unterstützt ausschließlich Agent A")
    with session.begin():
        agent = session.get(Agent, agent_id)
        if (
            agent is None
            or not agent.is_active
            or agent.scenario is AgentScenario.benchmark
        ):
            raise AgentRunError("Agent nicht gefunden, deaktiviert oder Benchmark")
        run = AgentRun(
            agent_id=agent_id,
            model=model,
            trigger=trigger,
            status=RunStatus.running,
            started_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        return run


def finish_agent_run(
    session: Session,
    run_id: UUID,
    *,
    status: RunStatus,
    model: str | None,
    provider_request_id: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: Decimal | None,
    error: str | None,
) -> AgentRun:
    if status is RunStatus.running:
        raise AgentRunError("Ein Agent-Run kann nicht erneut als laufend markiert werden")
    if status is RunStatus.succeeded and (
        input_tokens is None
        or output_tokens is None
        or cost_usd is None
    ):
        raise AgentRunError("Erfolgreiche Runs benötigen vollständige Nutzungsdaten")
    with session.begin():
        run = session.scalar(
            select(AgentRun).where(AgentRun.id == run_id).with_for_update()
        )
        if run is None or run.agent_id != "agent-a":
            raise AgentRunError("Agent-Run nicht gefunden")
        if run.status is not RunStatus.running:
            raise AgentRunError("Agent-Run ist bereits abgeschlossen")
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.model = model or run.model
        run.provider_request_id = provider_request_id
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.cost_usd = cost_usd
        run.error = error[:1000] if error else None
        session.flush()
        return run


def record_agent_decision(
    session: Session,
    *,
    agent_id: str,
    run_id: UUID,
    action: str,
    symbol: str | None,
    quantity: Decimal | None,
    rationale: str,
    investment_horizon_days: int,
    confidence: Decimal,
    source_ids: list[str],
    market_context: dict,
) -> Decision:
    decision_types = {
        "BUY": DecisionType.open,
        "SELL": DecisionType.close,
        "HOLD": DecisionType.no_trade,
        "NO_TRADE": DecisionType.no_trade,
    }
    if action not in decision_types:
        raise AgentRunError("Unbekannte Agentenentscheidung")
    if agent_id != "agent-a":
        raise AgentRunError("Iteration 4 unterstützt ausschließlich Agent A")
    with session.begin():
        run = session.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.agent_id == agent_id)
            .with_for_update()
        )
        if run is None or run.status is not RunStatus.running:
            raise AgentRunError("Aktiver Agent-Run nicht gefunden")
        agent = session.get(Agent, agent_id)
        if agent is None or not agent.is_active:
            raise AgentRunError("Agent nicht gefunden oder deaktiviert")
        verify_source_ids(session, source_ids, agent_id, run, market_context)
        instrument = {"symbol": symbol} if symbol else None
        if action in {"BUY", "SELL"}:
            if symbol is None or quantity is None or quantity <= 0:
                raise AgentRunError("BUY und SELL benötigen Instrument und positive Stückzahl")
            context = market_context.get("data_context") or {}
            candidates = [
                item for item in context.get("instruments", [])
                if isinstance(item, dict) and item.get("symbol") == symbol
                and f"market_data:{item.get('market_data_id')}" in source_ids
            ]
            if len(candidates) != 1:
                raise AgentRunError("Orderinstrument benötigt einen zitierten Kurs im Run-Kontext")
            quote = session.get(MarketData, UUID(candidates[0]["market_data_id"]))
            if quote is None or quote.symbol != symbol:
                raise AgentRunError("Kursquelle stimmt nicht mit dem Orderinstrument überein")
            instrument = {
                "symbol": symbol,
                "isin": (quote.meta or {}).get("isin"),
                "exchange": (quote.meta or {}).get("exchange"),
                "currency": quote.currency,
                "market_data_id": str(quote.id),
            }
        decision = Decision(
            agent_id=agent_id,
            agent_run_id=run_id,
            decision_type=decision_types[action],
            instrument=instrument,
            rationale=rationale,
            hypotheses={
                "action": action,
                "quantity": str(quantity) if quantity is not None else None,
                "investment_horizon_days": investment_horizon_days,
                "confidence": str(confidence),
                "source_ids": source_ids,
            },
            market_context=market_context,
        )
        session.add(decision)
        session.flush()
        return decision
