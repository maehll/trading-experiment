"""Portfolio-period evaluation with immutable, reproducible reward records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading_core.models import (
    Agent,
    AgentRun,
    Execution,
    MarketData,
    Order,
    PortfolioSnapshot,
    Reward,
    RunStatus,
)
from trading_core.reward_config import RewardConfig

ZERO = Decimal("0")


class RewardEvaluationError(RuntimeError):
    """Reward inputs are incomplete, stale, or not comparable."""


def _validate_period(period_start: datetime, period_end: datetime) -> None:
    if (
        period_start.tzinfo is None
        or period_start.utcoffset() is None
        or period_end.tzinfo is None
        or period_end.utcoffset() is None
    ):
        raise ValueError("Reward-Zeiträume müssen zeitzonenbewusst sein")
    if period_start >= period_end:
        raise ValueError("period_start muss vor period_end liegen")


def _snapshot_at_or_before(
    session: Session, agent_id: str, as_of: datetime
) -> PortfolioSnapshot:
    snapshot = session.scalar(
        select(PortfolioSnapshot)
        .where(
            PortfolioSnapshot.agent_id == agent_id,
            PortfolioSnapshot.as_of <= as_of,
        )
        .order_by(PortfolioSnapshot.as_of.desc())
        .limit(1)
    )
    if snapshot is None:
        raise RewardEvaluationError(
            f"Kein Portfolio-Snapshot für {agent_id} bis {as_of.isoformat()}"
        )
    return snapshot


def _model_costs(
    session: Session,
    agent_id: str,
    period_start: datetime,
    period_end: datetime,
) -> tuple[Decimal, list[AgentRun]]:
    runs = list(
        session.scalars(
            select(AgentRun)
            .where(AgentRun.agent_id == agent_id, AgentRun.started_at < period_end)
            .order_by(AgentRun.started_at, AgentRun.id)
            .with_for_update()
        )
    )
    costs = ZERO
    included: list[AgentRun] = []
    for run in runs:
        if run.status is RunStatus.running:
            raise RewardEvaluationError(
                f"Agent-Run {run.id} ist noch nicht abgeschlossen"
            )
        if period_start <= run.started_at < period_end:
            if run.finished_at is None or run.cost_usd is None:
                raise RewardEvaluationError(
                    f"Agent-Run {run.id} hat keinen Abschluss oder vollständige Modellkosten"
                )
            if not run.cost_usd.is_finite() or run.cost_usd < ZERO:
                raise RewardEvaluationError(f"Agent-Run {run.id} hat ungültige Modellkosten")
            costs += run.cost_usd
            included.append(run)
    return costs, included


def _usd_to_eur_rate(
    session: Session, config: RewardConfig, period_end: datetime
) -> tuple[Decimal, MarketData]:
    quote = session.scalar(
        select(MarketData)
        .where(
            MarketData.symbol == config.usd_eur_symbol,
            MarketData.as_of <= period_end,
            MarketData.fetched_at <= period_end,
        )
        .order_by(MarketData.as_of.desc(), MarketData.fetched_at.desc())
        .limit(1)
    )
    if quote is None:
        raise RewardEvaluationError(
            f"Kein zeitgerechter FX-Kurs für {config.usd_eur_symbol}"
        )
    if quote.quality.value != "ok":
        raise RewardEvaluationError("FX-Marktdaten sind nicht als valide markiert")
    if (
        quote.currency != "USD"
        or quote.price <= ZERO
        or (quote.meta or {}).get("instrument_type") != "fx"
        or (quote.meta or {}).get("rate_convention") != "USD_per_EUR"
    ):
        raise RewardEvaluationError(
            f"{config.usd_eur_symbol} muss als positiver USD-pro-EUR-FX-Kurs markiert sein"
        )
    age = period_end - quote.as_of
    if (
        age < timedelta(0)
        or age > timedelta(seconds=config.usd_eur_max_age_seconds)
        or quote.fetched_at > period_end
    ):
        raise RewardEvaluationError("FX-Kurs ist für den Reward-Zeitraum veraltet")
    return quote.price, quote


def _turnover(
    session: Session, agent_id: str, period_start: datetime, period_end: datetime
) -> tuple[Decimal, list[Execution]]:
    fills = list(
        session.scalars(
            select(Execution)
            .join(Order, Order.id == Execution.order_id)
            .where(
                Order.agent_id == agent_id,
                Execution.executed_at > period_start,
                Execution.executed_at <= period_end,
            )
            .order_by(Execution.executed_at, Execution.id)
        )
    )
    return sum((fill.quantity * fill.price for fill in fills), ZERO), fills


def _snapshot_audit(snapshot: PortfolioSnapshot) -> dict[str, object]:
    metrics = snapshot.metrics or {}
    market_data = metrics.get("market_data")
    if market_data is None:
        market_data_id = metrics.get("market_data_id")
        market_data = (
            [
                {
                    "market_data_id": market_data_id,
                    "as_of": metrics.get("market_data_as_of"),
                }
            ]
            if market_data_id is not None
            else []
        )
    return {
        "snapshot_id": str(snapshot.id),
        "as_of": snapshot.as_of.isoformat(),
        "total_value_eur": str(snapshot.total_value_eur),
        "cash_eur": str(snapshot.cash_eur),
        "positions_value_eur": str(snapshot.positions_value_eur),
        "market_data": market_data,
    }


def _period_drawdown(
    session: Session, agent_id: str, start: PortfolioSnapshot, period_end: datetime
) -> tuple[Decimal, list[PortfolioSnapshot]]:
    snapshots = list(
        session.scalars(
            select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.agent_id == agent_id,
                PortfolioSnapshot.as_of > start.as_of,
                PortfolioSnapshot.as_of <= period_end,
            )
            .order_by(PortfolioSnapshot.as_of, PortfolioSnapshot.id)
        )
    )
    peak = start.total_value_eur
    maximum = ZERO
    for snapshot in snapshots:
        if snapshot.total_value_eur <= ZERO:
            raise RewardEvaluationError("Portfolio-Snapshot hat keinen positiven Gesamtwert")
        peak = max(peak, snapshot.total_value_eur)
        maximum = max(maximum, (peak - snapshot.total_value_eur) / peak * 100)
    return maximum, snapshots


def evaluate_reward(
    session: Session,
    agent_id: str,
    period_start: datetime,
    period_end: datetime,
    reward_config: RewardConfig,
    *,
    benchmark_agent_id: str = "benchmark-d",
) -> Reward:
    """Evaluate once per (agent, period, version); never overwrite a result."""
    _validate_period(period_start, period_end)
    if agent_id == benchmark_agent_id:
        raise ValueError("Der passive Benchmark erhält keinen Agenten-Reward")

    lock_key = f"reward:{agent_id}:{period_start.isoformat()}:{period_end.isoformat()}:{reward_config.version}"
    with session.begin():
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": lock_key},
        )
        existing = session.scalar(
            select(Reward).where(
                Reward.agent_id == agent_id,
                Reward.period_start == period_start,
                Reward.period_end == period_end,
                Reward.reward_version == reward_config.version,
            )
        )
        if existing is not None:
            return existing

        agent = session.get(Agent, agent_id)
        if agent is None:
            raise RewardEvaluationError(f"Agent nicht gefunden: {agent_id}")
        start = _snapshot_at_or_before(session, agent_id, period_start)
        end = _snapshot_at_or_before(session, agent_id, period_end)
        benchmark_start = _snapshot_at_or_before(
            session, benchmark_agent_id, period_start
        )
        benchmark_end = _snapshot_at_or_before(session, benchmark_agent_id, period_end)
        if end.as_of <= start.as_of:
            raise RewardEvaluationError("Kein späterer Agenten-Snapshot im Zeitraum")
        if benchmark_end.as_of <= benchmark_start.as_of:
            raise RewardEvaluationError("Kein späterer Benchmark-Snapshot im Zeitraum")
        if start.total_value_eur <= ZERO or benchmark_start.total_value_eur <= ZERO:
            raise RewardEvaluationError("Startwert des Portfolios muss positiv sein")
        if reward_config.version == "v1" and (start.drawdown_pct is None or end.drawdown_pct is None):
            raise RewardEvaluationError("Drawdown-Daten für den Zeitraum fehlen")

        model_cost_usd, cost_runs = _model_costs(
            session, agent_id, period_start, period_end
        )
        model_cost_eur = ZERO
        fx_quote: MarketData | None = None
        if model_cost_usd > ZERO:
            usd_per_eur, fx_quote = _usd_to_eur_rate(
                session, reward_config, period_end
            )
            model_cost_eur = model_cost_usd / usd_per_eur

        raw_return = (end.total_value_eur - start.total_value_eur) / start.total_value_eur
        net_return = (
            end.total_value_eur - start.total_value_eur - model_cost_eur
        ) / start.total_value_eur
        benchmark_return = (
            benchmark_end.total_value_eur - benchmark_start.total_value_eur
        ) / benchmark_start.total_value_eur
        if reward_config.version == "v1":
            drawdown_penalty = max(ZERO, end.drawdown_pct - start.drawdown_pct)
            drawdown_snapshots: list[PortfolioSnapshot] = []
        else:
            drawdown_penalty, drawdown_snapshots = _period_drawdown(
                session, agent_id, start, period_end
            )
        turnover_eur, fills = _turnover(
            session, agent_id, period_start, period_end
        )
        turnover_pct = turnover_eur / start.total_value_eur * 100
        if reward_config.version == "v1":
            reward_value = (
                Decimal("100")
                * (net_return - Decimal(str(reward_config.lambda_benchmark)) * benchmark_return)
                - Decimal(str(reward_config.lambda_drawdown)) * drawdown_penalty
                - Decimal(str(reward_config.lambda_turnover)) * turnover_pct
            )
        else:
            reward_value = (
                Decimal("100") * net_return
                - Decimal("0.5") * drawdown_penalty
                - Decimal("0.05") * turnover_pct
            )
        reward = Reward(
            agent_id=agent_id,
            period_start=period_start,
            period_end=period_end,
            reward_version=reward_config.version,
            net_return=net_return,
            benchmark_return=benchmark_return,
            drawdown_increase_pct=drawdown_penalty,
            turnover_pct=turnover_pct,
            reward_value=reward_value,
            components={
                "formula": (
                    "100*(r-lambda_b*b)-lambda_d*delta_dd-lambda_u*turnover"
                    if reward_config.version == "v1"
                    else "100*net_return-0.5*max_period_drawdown_pct-0.05*turnover_pct"
                ),
                "formula_version": reward_config.version,
                "portfolio_return_before_model_costs": str(raw_return),
                "model_cost_usd": str(model_cost_usd),
                "model_cost_eur": str(model_cost_eur),
                "fx_market_data_id": str(fx_quote.id) if fx_quote else None,
                "fx_as_of": fx_quote.as_of.isoformat() if fx_quote else None,
                "fx_fetched_at": (
                    fx_quote.fetched_at.isoformat() if fx_quote else None
                ),
                "fx_usd_per_eur": str(fx_quote.price) if fx_quote else None,
                "fx_source": fx_quote.source if fx_quote else None,
                "start_snapshot_id": str(start.id),
                "end_snapshot_id": str(end.id),
                "benchmark_agent_id": benchmark_agent_id,
                "benchmark_start_snapshot_id": str(benchmark_start.id),
                "benchmark_end_snapshot_id": str(benchmark_end.id),
                "agent_start": _snapshot_audit(start),
                "agent_end": _snapshot_audit(end),
                "benchmark_start": _snapshot_audit(benchmark_start),
                "benchmark_end": _snapshot_audit(benchmark_end),
                "turnover_eur": str(turnover_eur),
                "execution_count": len(fills),
                "executions": [
                    {
                        "execution_id": str(fill.id),
                        "order_id": str(fill.order_id),
                        "executed_at": fill.executed_at.isoformat(),
                        "quantity": str(fill.quantity),
                        "price": str(fill.price),
                        "fee": str(fill.fee),
                        "fee_currency": fill.fee_currency,
                        "market_data_id": str(fill.market_data_id) if fill.market_data_id else None,
                        "simulation_details": fill.simulation_details,
                    }
                    for fill in fills
                ],
                "model_cost_runs": [
                    {
                        "run_id": str(run.id),
                        "started_at": run.started_at.isoformat(),
                        "finished_at": run.finished_at.isoformat(),
                        "cost_usd": str(run.cost_usd),
                    }
                    for run in cost_runs
                ],
                "drawdown_penalty_pct": str(drawdown_penalty),
                "drawdown_snapshots": [_snapshot_audit(s) for s in drawdown_snapshots],
                "lambda_benchmark": reward_config.lambda_benchmark,
                "lambda_drawdown": reward_config.lambda_drawdown,
                "lambda_turnover": reward_config.lambda_turnover,
                "effective_benchmark_weight": reward_config.lambda_benchmark if reward_config.version == "v1" else 0,
                "effective_drawdown_weight": reward_config.lambda_drawdown if reward_config.version == "v1" else 0.5,
                "effective_turnover_weight": reward_config.lambda_turnover if reward_config.version == "v1" else 0.05,
            },
        )
        session.add(reward)
        session.flush()
        return reward
