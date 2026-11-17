"""Filtered read-only market, research, portfolio, and reward queries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trading_core.models import (
    Agent,
    AgentScenario,
    DataQuality,
    MarketData,
    Position,
    ResearchItem,
    Reward,
    SimulatorAccount,
)


class DataUnavailable(RuntimeError):
    """Requested context is missing or cannot be valued safely."""


def _quote_query(now: datetime, max_age_seconds: int):
    oldest = now - timedelta(seconds=max_age_seconds)
    ranked_quotes = (
        select(
            MarketData.id,
            MarketData.symbol,
            MarketData.source,
            MarketData.currency,
            MarketData.price,
            MarketData.bid,
            MarketData.ask,
            MarketData.as_of,
            MarketData.fetched_at,
            MarketData.quality,
            MarketData.meta,
            func.row_number()
            .over(
                partition_by=MarketData.symbol,
                order_by=(MarketData.as_of.desc(), MarketData.fetched_at.desc()),
            )
            .label("quote_rank"),
        )
        .where(
            MarketData.as_of <= now,
            MarketData.fetched_at <= now,
            MarketData.as_of >= oldest,
            MarketData.quality == DataQuality.ok,
            MarketData.currency == "EUR",
            MarketData.price > 0,
        )
    ).subquery()
    return (
        select(ranked_quotes)
        .where(ranked_quotes.c.quote_rank == 1)
        .order_by(ranked_quotes.c.fetched_at.desc(), ranked_quotes.c.as_of.desc())
        .limit(201)
    )


def _instrument(row) -> dict[str, object] | None:
    metadata = row.meta or {}
    kind = metadata.get("instrument_type")
    if kind not in {"stock", "etf"}:
        return None
    if row.bid is not None and row.bid <= 0:
        return None
    if row.ask is not None and row.ask <= 0:
        return None
    if row.bid is not None and row.ask is not None and row.bid > row.ask:
        return None
    return {
        "market_data_id": str(row.id),
        "symbol": row.symbol,
        "instrument_type": kind,
        "name": metadata.get("name"),
        "isin": metadata.get("isin"),
        "exchange": metadata.get("exchange"),
        "currency": row.currency,
        "price": str(row.price),
        "bid": str(row.bid) if row.bid is not None else None,
        "ask": str(row.ask) if row.ask is not None else None,
        "as_of": row.as_of.isoformat(),
        "fetched_at": row.fetched_at.isoformat(),
        "source": row.source,
    }


def get_agent_context(
    session: Session,
    agent_id: str,
    *,
    max_age_seconds: int,
    now: datetime | None = None,
) -> dict[str, object]:
    now = now or datetime.now(UTC)
    agent_row = session.execute(
        select(Agent.id, Agent.is_active, Agent.scenario).where(Agent.id == agent_id)
    ).one_or_none()
    if agent_row is None or not agent_row.is_active:
        raise DataUnavailable("Agent nicht gefunden oder deaktiviert")
    if agent_row.scenario is AgentScenario.benchmark:
        raise DataUnavailable("Benchmark-Agenten erhalten keinen Agentenkontext")

    instrument_rows = session.execute(_quote_query(now, max_age_seconds)).all()
    instruments = [
        instrument for row in instrument_rows if (instrument := _instrument(row))
    ]
    instruments.sort(
        key=lambda instrument: (instrument["fetched_at"], instrument["as_of"]),
        reverse=True,
    )
    truncated = len(instruments) > 200
    instruments = instruments[:200]
    quotes_by_symbol = {item["symbol"]: item for item in instruments}

    research_rows = session.execute(
        select(
            ResearchItem.id,
            ResearchItem.agent_id,
            ResearchItem.source,
            ResearchItem.url,
            ResearchItem.title,
            ResearchItem.summary,
            ResearchItem.content,
            ResearchItem.published_at,
            ResearchItem.fetched_at,
            ResearchItem.tags,
        )
        .where(
            or_(ResearchItem.agent_id.is_(None), ResearchItem.agent_id == agent_id),
            ResearchItem.fetched_at >= now - timedelta(days=7),
            ResearchItem.fetched_at <= now,
            or_(ResearchItem.published_at.is_(None), ResearchItem.published_at <= now),
        )
        .order_by(ResearchItem.fetched_at.desc())
        .limit(10)
    ).all()
    research = [
        {
            "research_item_id": str(row.id),
            "source": row.source,
            "url": row.url,
            "title": row.title,
            "summary": (row.summary or "")[:800],
            "content": (row.content or "")[:800],
            "published_at": row.published_at.isoformat()
            if row.published_at
            else None,
            "fetched_at": row.fetched_at.isoformat(),
            "tags": row.tags or {},
        }
        for row in research_rows
    ]

    account_cash = session.scalar(
        select(SimulatorAccount.cash_eur).where(SimulatorAccount.agent_id == agent_id)
    )
    position_rows = session.execute(
        select(
            Position.symbol,
            Position.currency,
            Position.quantity,
            Position.avg_entry_price,
            Position.opened_at,
        ).where(
            Position.agent_id == agent_id,
            Position.closed_at.is_(None),
        )
    ).all()
    positions: list[dict[str, object]] = []
    missing_position_quotes: list[str] = []
    positions_value = Decimal("0")
    for row in position_rows:
        quote = quotes_by_symbol.get(row.symbol)
        market_value = (
            Decimal(quote["price"]) * row.quantity
            if quote is not None and row.currency == "EUR"
            else None
        )
        if market_value is None:
            missing_position_quotes.append(row.symbol)
        else:
            positions_value += market_value
        positions.append(
            {
                "symbol": row.symbol,
                "currency": row.currency,
                "quantity": str(row.quantity),
                "avg_entry_price_eur": str(row.avg_entry_price),
                "market_value_eur": str(market_value) if market_value is not None else None,
                "market_data_id": quote["market_data_id"] if quote else None,
                "market_data_as_of": quote["as_of"] if quote else None,
                "opened_at": row.opened_at.isoformat(),
            }
        )
    portfolio_complete = account_cash is not None and not missing_position_quotes
    portfolio = {
        "cash_eur": str(account_cash) if account_cash is not None else None,
        "positions": positions,
        "positions_value_eur": str(positions_value) if portfolio_complete else None,
        "equity_eur": (
            str(account_cash + positions_value) if portfolio_complete else None
        ),
        "complete": portfolio_complete,
        "unpriced_symbols": missing_position_quotes,
    }

    reward_rows = session.execute(
        select(
            Reward.period_start,
            Reward.period_end,
            Reward.reward_version,
            Reward.net_return,
            Reward.benchmark_return,
            Reward.drawdown_increase_pct,
            Reward.turnover_pct,
            Reward.reward_value,
            Reward.created_at,
        )
        .where(Reward.agent_id == agent_id)
        .order_by(Reward.period_end.desc(), Reward.created_at.desc())
        .limit(10)
    ).all()
    rewards = [
        {
            "period_start": row.period_start.isoformat(),
            "period_end": row.period_end.isoformat(),
            "formula_version": row.reward_version,
            "net_return": str(row.net_return),
            "benchmark_return": str(row.benchmark_return)
            if row.benchmark_return is not None
            else None,
            "drawdown_increase_pct": str(row.drawdown_increase_pct)
            if row.drawdown_increase_pct is not None
            else None,
            "turnover_pct": str(row.turnover_pct)
            if row.turnover_pct is not None
            else None,
            "reward_value": str(row.reward_value),
            "created_at": row.created_at.isoformat(),
        }
        for row in reward_rows
    ]

    fx_row = session.execute(
        select(
            MarketData.id,
            MarketData.price,
            MarketData.as_of,
            MarketData.fetched_at,
            MarketData.currency,
            MarketData.quality,
            MarketData.meta,
        )
        .where(
            MarketData.symbol == "EURUSD",
            MarketData.as_of <= now,
            MarketData.fetched_at <= now,
            MarketData.as_of >= now - timedelta(days=3),
            MarketData.quality == DataQuality.ok,
        )
        .order_by(MarketData.as_of.desc(), MarketData.fetched_at.desc())
        .limit(1)
    ).one_or_none()
    fx = None
    if (
        fx_row is not None
        and fx_row.currency == "USD"
        and fx_row.price > 0
        and (fx_row.meta or {}).get("instrument_type") == "fx"
        and (fx_row.meta or {}).get("rate_convention") == "USD_per_EUR"
    ):
        fx = {
            "market_data_id": str(fx_row.id),
            "usd_per_eur": str(fx_row.price),
            "as_of": fx_row.as_of.isoformat(),
            "fetched_at": fx_row.fetched_at.isoformat(),
        }

    return {
        "agent_id": agent_id,
        "captured_at": now.isoformat(),
        "instruments": instruments,
        "universe_truncated": truncated,
        "research": research,
        "portfolio": portfolio,
        "recent_rewards": rewards,
        "budget_fx": fx,
    }
