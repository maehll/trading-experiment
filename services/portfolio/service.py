"""EUR portfolio valuation from the single trusted market-data store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trading_core.models import (
    Agent,
    AgentScenario,
    AgentVariant,
    BenchmarkPortfolio,
    MarketData,
    PortfolioSnapshot,
    Position,
    SimulatorAccount,
)
from trading_core.risk import RiskConfig

from services.order_gateway.risk_engine import RiskEngine
from services.order_gateway.execution import touch_price
from trading_core.models import OrderSide

ZERO = Decimal("0")


class PortfolioValuationError(RuntimeError):
    """A complete, time-correct EUR portfolio value could not be calculated."""


def _validate_time(as_of: datetime) -> None:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Portfolio-Zeitpunkte müssen eine Zeitzone enthalten")


def _market_data_audit(quote: MarketData) -> dict[str, str]:
    return {
        "symbol": quote.symbol,
        "market_data_id": str(quote.id),
        "as_of": quote.as_of.isoformat(),
        "fetched_at": quote.fetched_at.isoformat(),
        "price": str(quote.price),
        "currency": quote.currency,
        "quality": quote.quality.value,
        "source": quote.source,
    }


def _benchmark_snapshot(
    session: Session,
    agent: Agent,
    benchmark: BenchmarkPortfolio,
    risk_policy: RiskConfig,
    as_of: datetime,
) -> PortfolioSnapshot:
    existing = session.scalar(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.agent_id == agent.id,
            PortfolioSnapshot.as_of == as_of,
        )
    )
    if existing is not None:
        return existing

    quote, error = RiskEngine(risk_policy).latest_quote(
        session, benchmark.symbol, as_of
    )
    if error or quote is None or quote.currency != benchmark.currency:
        raise PortfolioValuationError(
            f"Benchmark {benchmark.symbol} nicht bewertbar: {error or 'Währungskonflikt'}"
        )
    positions_value = benchmark.units * quote.price
    total_value = benchmark.cash_eur + positions_value
    previous_high = session.scalar(
        select(func.max(PortfolioSnapshot.total_value_eur)).where(
            PortfolioSnapshot.agent_id == agent.id,
            PortfolioSnapshot.as_of <= as_of,
        )
    )
    high_watermark = max(
        benchmark.initial_capital_eur,
        Decimal(str(previous_high)) if previous_high is not None else ZERO,
        total_value,
    )
    drawdown_pct = (
        max(ZERO, (high_watermark - total_value) / high_watermark * 100)
        if high_watermark > ZERO
        else ZERO
    )
    snapshot = PortfolioSnapshot(
        agent_id=agent.id,
        as_of=as_of,
        total_value_eur=total_value,
        cash_eur=benchmark.cash_eur,
        positions_value_eur=positions_value,
        unrealized_pnl_eur=benchmark.units
        * (quote.price - benchmark.entry_price_eur),
        realized_pnl_eur=ZERO,
        high_watermark_eur=high_watermark,
        drawdown_pct=drawdown_pct,
        metrics={
            "portfolio_type": "passive_benchmark",
            "symbol": benchmark.symbol,
            "units": str(benchmark.units),
            "market_data_id": str(quote.id),
            "market_data_as_of": quote.as_of.isoformat(),
            "market_data": [_market_data_audit(quote)],
        },
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def capture_snapshot_in_transaction(
    session: Session,
    agent_id: str,
    risk_policy: RiskConfig,
    as_of: datetime | None = None,
) -> PortfolioSnapshot:
    """Capture a snapshot using the caller's existing transaction."""
    as_of = as_of or datetime.now(UTC)
    _validate_time(as_of)

    agent = session.get(Agent, agent_id)
    if agent is None:
        raise PortfolioValuationError(f"Agent nicht gefunden: {agent_id}")
    if agent.scenario is AgentScenario.benchmark:
        benchmark = session.get(BenchmarkPortfolio, agent_id)
        if benchmark is None:
            raise PortfolioValuationError(
                f"Benchmark-Portfolio ist nicht initialisiert: {agent_id}"
            )
        return _benchmark_snapshot(session, agent, benchmark, risk_policy, as_of)

    existing = session.scalar(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.agent_id == agent_id,
            PortfolioSnapshot.as_of == as_of,
        )
    )
    if existing is not None:
        return existing
    account = session.scalar(
        select(SimulatorAccount)
        .where(SimulatorAccount.agent_id == agent_id)
        .with_for_update()
    )
    if account is None:
        raise PortfolioValuationError(
            f"Simulator-Konto ist nicht initialisiert: {agent_id}"
        )

    positions = list(
        session.scalars(
            select(Position)
            .where(Position.agent_id == agent_id)
            .with_for_update()
        )
    )
    active_positions = [position for position in positions if position.closed_at is None]
    engine = RiskEngine(risk_policy)
    positions_value = ZERO
    unrealized_pnl = ZERO
    quote_audit: list[dict[str, str]] = []
    for position in active_positions:
        quote, error = engine.latest_quote(session, position.symbol, as_of)
        if error or quote is None:
            raise PortfolioValuationError(
                f"Position {position.symbol} nicht bewertbar: {error or 'Kurs fehlt'}"
            )
        if quote.currency != position.currency or quote.currency != "EUR":
            raise PortfolioValuationError(
                f"Position {position.symbol} hat eine nicht unterstützte Währung"
            )
        positions_value += position.quantity * quote.price
        unrealized_pnl += position.quantity * (quote.price - position.avg_entry_price)
        quote_audit.append(_market_data_audit(quote))

    total_value = account.cash_eur + positions_value
    if total_value <= ZERO:
        raise PortfolioValuationError("Portfolio-Gesamtwert ist nicht positiv")
    if account.risk_day != as_of.date():
        account.risk_day = as_of.date()
        account.day_start_equity_eur = max(account.last_equity_eur, total_value)
    high_watermark = max(account.high_watermark_eur, total_value)
    drawdown_pct = max(
        ZERO, (high_watermark - total_value) / high_watermark * 100
    )
    account.high_watermark_eur = high_watermark
    account.last_equity_eur = total_value
    account.updated_at = as_of

    snapshot = PortfolioSnapshot(
        agent_id=agent_id,
        as_of=as_of,
        total_value_eur=total_value,
        cash_eur=account.cash_eur,
        positions_value_eur=positions_value,
        unrealized_pnl_eur=unrealized_pnl,
        realized_pnl_eur=sum((p.realized_pnl for p in positions), ZERO),
        high_watermark_eur=high_watermark,
        drawdown_pct=drawdown_pct,
        metrics={
            "portfolio_type": "paper",
            "open_position_count": len(active_positions),
            "currency": "EUR",
            "market_data": quote_audit,
        },
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def ensure_initial_snapshot(
    session: Session,
    agent_id: str,
    risk_policy: RiskConfig,
    as_of: datetime,
) -> PortfolioSnapshot:
    """Create the pre-trade starting point once, for later period comparisons."""
    _validate_time(as_of)
    first_snapshot = session.scalar(
        select(PortfolioSnapshot.id)
        .where(PortfolioSnapshot.agent_id == agent_id)
        .limit(1)
    )
    if first_snapshot is not None:
        snapshot = session.scalar(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.agent_id == agent_id)
            .order_by(PortfolioSnapshot.as_of)
            .limit(1)
        )
        assert snapshot is not None
        return snapshot
    return capture_snapshot_in_transaction(
        session,
        agent_id,
        risk_policy,
        as_of=as_of - timedelta(microseconds=1),
    )


def capture_snapshot(
    session: Session,
    agent_id: str,
    risk_policy: RiskConfig,
    as_of: datetime | None = None,
) -> PortfolioSnapshot:
    with session.begin():
        return capture_snapshot_in_transaction(session, agent_id, risk_policy, as_of)


def capture_all_snapshots(
    session_factory: sessionmaker[Session],
    risk_policy: RiskConfig,
    as_of: datetime | None = None,
) -> list[PortfolioSnapshot]:
    """One-shot daily valuation entry point; scheduling is left to the operator."""
    as_of = as_of or datetime.now(UTC)
    with session_factory() as session:
        agent_ids = list(
            session.scalars(
                select(SimulatorAccount.agent_id).order_by(SimulatorAccount.agent_id)
            )
        )
        agent_ids.extend(
            agent_id
            for agent_id in session.scalars(
                select(BenchmarkPortfolio.agent_id).order_by(
                    BenchmarkPortfolio.agent_id
                )
            )
            if agent_id not in agent_ids
        )
    snapshots = []
    for agent_id in agent_ids:
        with session_factory() as session:
            snapshots.append(
                capture_snapshot(session, agent_id, risk_policy, as_of=as_of)
            )
    return snapshots


def initialize_benchmark(
    session: Session,
    symbol: str,
    risk_policy: RiskConfig,
    *,
    agent_id: str = "benchmark-d",
    as_of: datetime | None = None,
) -> BenchmarkPortfolio:
    """Initialize a passive ETF buy-and-hold portfolio from a verified quote."""
    as_of = as_of or datetime.now(UTC)
    _validate_time(as_of)
    with session.begin():
        existing = session.get(BenchmarkPortfolio, agent_id)
        if existing is not None:
            if existing.symbol != symbol:
                raise ValueError(
                    "Benchmark ist bereits mit einem anderen Instrument initialisiert"
                )
            return existing

        quote, error = RiskEngine(risk_policy).latest_quote(session, symbol, as_of)
        if error or quote is None:
            raise PortfolioValuationError(
                f"Benchmark-Instrument nicht bewertbar: {error or 'Kurs fehlt'}"
            )
        if quote.currency != "EUR" or (quote.meta or {}).get("instrument_type") != "etf":
            raise PortfolioValuationError(
                "Passiver Benchmark erfordert ein eindeutig identifiziertes EUR-ETF"
            )

        agent = session.get(Agent, agent_id)
        if agent is None:
            agent = Agent(
                id=agent_id,
                name="passive-world-etf-benchmark",
                scenario=AgentScenario.benchmark,
                variant=AgentVariant.passive,
                config={"symbol": symbol, "method": "buy_and_hold"},
            )
            session.add(agent)
            session.flush()
        elif (
            agent.scenario is not AgentScenario.benchmark
            or agent.variant is not AgentVariant.passive
        ):
            raise ValueError("Benchmark-Agent hat eine inkompatible Identität")

        capital = Decimal(str(risk_policy.starting_capital_eur))
        touch = touch_price(OrderSide.buy, quote, risk_policy.simulator)
        execution_price = touch * (
            Decimal("1")
            + Decimal(str(risk_policy.simulator.slippage_bps)) / Decimal("10000")
        )
        fee_rate = Decimal(str(risk_policy.simulator.fee_bps)) / Decimal("10000")
        units = (capital / (execution_price * (Decimal("1") + fee_rate))).quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN
        )
        fee = units * execution_price * fee_rate
        cash = capital - units * execution_price - fee
        benchmark = BenchmarkPortfolio(
            agent_id=agent_id,
            symbol=symbol,
            currency=quote.currency,
            units=units,
            entry_price_eur=execution_price,
            cash_eur=cash,
            initial_capital_eur=capital,
            entry_market_data_id=quote.id,
            started_at=as_of,
        )
        session.add(benchmark)
        session.flush()
        session.add(
            PortfolioSnapshot(
                agent_id=agent_id,
                as_of=as_of - timedelta(microseconds=1),
                total_value_eur=capital,
                cash_eur=capital,
                positions_value_eur=ZERO,
                unrealized_pnl_eur=ZERO,
                realized_pnl_eur=ZERO,
                high_watermark_eur=capital,
                drawdown_pct=ZERO,
                metrics={"portfolio_type": "passive_benchmark", "event": "initial_capital"},
            )
        )
        session.flush()
        capture_snapshot_in_transaction(session, agent_id, risk_policy, as_of)
        return benchmark
