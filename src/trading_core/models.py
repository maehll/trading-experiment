"""Datenmodell des Trading-Experiments (14 Kerntabellen, Spez §9.1).

Unveränderlichkeit (Append-only) wird nicht hier, sondern durch
Datenbank-Trigger in den Alembic-Migrationen erzwungen, damit sie auch
für direkte SQL-Zugriffe gilt.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# --- Enums -----------------------------------------------------------------


class AgentScenario(enum.Enum):
    free = "free"
    intraday = "intraday"
    long_term = "long_term"
    benchmark = "benchmark"


class AgentVariant(enum.Enum):
    adaptive = "adaptive"
    frozen = "frozen"
    passive = "passive"


class RunStatus(enum.Enum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class StrategyStatus(enum.Enum):
    draft = "draft"
    simulated = "simulated"
    active = "active"
    retired = "retired"


class DecisionType(enum.Enum):
    open = "open"
    close = "close"
    adjust = "adjust"
    rebalance = "rebalance"
    no_trade = "no_trade"


class OrderSide(enum.Enum):
    buy = "buy"
    sell = "sell"


class OrderType(enum.Enum):
    market = "market"
    limit = "limit"


class OrderStatus(enum.Enum):
    pending_risk = "pending_risk"
    risk_approved = "risk_approved"
    risk_rejected = "risk_rejected"
    submitted = "submitted"
    partially_filled = "partially_filled"
    filled = "filled"
    cancelled = "cancelled"
    error = "error"


class DataQuality(enum.Enum):
    ok = "ok"
    stale = "stale"
    suspect = "suspect"


class RiskSeverity(enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


# --- Tabellen ---------------------------------------------------------------


class Agent(Base):
    """Agentenidentität, Szenario und Konfiguration."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # z. B. "agent-a"
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scenario: Mapped[AgentScenario] = mapped_column(
        Enum(AgentScenario, name="agent_scenario"), nullable=False
    )
    variant: Mapped[AgentVariant] = mapped_column(
        Enum(AgentVariant, name="agent_variant"), nullable=False
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()


class AgentRun(Base):
    """Einzelne KI-Ausführungen, Laufzeit und Modellkosten."""

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)  # scheduled/event/manual
    model: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), nullable=False, default=RunStatus.running
    )
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_agent_runs_agent_started", "agent_id", "started_at"),)


class Strategy(Base):
    """Strategiedefinitionen und Versionen.

    Inhaltliche Felder sind nach dem Anlegen unveränderlich (DB-Trigger);
    nur status/activated_at/retired_at dürfen sich ändern.
    """

    __tablename__ = "strategies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategies.id")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    data_used: Mapped[dict | None] = mapped_column(JSONB)
    prior_performance: Mapped[dict | None] = mapped_column(JSONB)
    expected_effect: Mapped[str | None] = mapped_column(Text)
    simulation_result: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[StrategyStatus] = mapped_column(
        Enum(StrategyStatus, name="strategy_status"),
        nullable=False,
        default=StrategyStatus.draft,
    )
    created_at: Mapped[datetime] = _created_at()
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("agent_id", "version"),)


class Decision(Base):
    """Entscheidungen einschließlich Nichthandeln. Append-only."""

    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("strategies.id"))
    decision_type: Mapped[DecisionType] = mapped_column(
        Enum(DecisionType, name="decision_type"), nullable=False
    )
    instrument: Mapped[dict | None] = mapped_column(JSONB)  # symbol, isin, exchange …
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    hypotheses: Mapped[dict | None] = mapped_column(JSONB)
    market_context: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_decisions_agent_created", "agent_id", "created_at"),)


class Order(Base):
    """Orderanforderungen und Brokerstatus."""

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    client_order_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("decisions.id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    isin: Mapped[str | None] = mapped_column(Text)
    exchange: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, name="order_side"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, name="order_type"), nullable=False
    )
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    time_in_force: Mapped[str] = mapped_column(Text, nullable=False, default="day")
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.pending_risk,
    )
    risk_check: Mapped[dict | None] = mapped_column(JSONB)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("ix_orders_agent_created", "agent_id", "created_at"),)


class Execution(Base):
    """Tatsächliche Ausführungen und Gebühren. Append-only."""

    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), nullable=False)
    broker_execution_id: Mapped[str] = mapped_column(Text, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    fee_currency: Mapped[str | None] = mapped_column(Text)
    market_data_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("market_data.id"))
    simulation_details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("order_id", "broker_execution_id"),)


class Position(Base):
    """Aktuelle und historische Positionen."""

    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=0
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_positions_agent_symbol", "agent_id", "symbol"),
        Index(
            "uq_positions_open_agent_symbol",
            "agent_id",
            "symbol",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )


class PortfolioSnapshot(Base):
    """Vermögenswerte und Risikokennzahlen. Append-only."""

    __tablename__ = "portfolio_snapshots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_value_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cash_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    positions_value_eur: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False
    )
    unrealized_pnl_eur: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    realized_pnl_eur: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    high_watermark_eur: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("agent_id", "as_of"),)


class MarketData(Base):
    """Verwendete Kursinformationen (einzige verbindliche Preisquelle)."""

    __tablename__ = "market_data"

    id: Mapped[uuid.UUID] = _uuid_pk()
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )  # Zeitpunkt der Kursdaten
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    quality: Mapped[DataQuality] = mapped_column(
        Enum(DataQuality, name="data_quality"),
        nullable=False,
        default=DataQuality.ok,
    )
    meta: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (Index("ix_market_data_symbol_asof", "symbol", "as_of"),)


class BenchmarkPortfolio(Base):
    """Unveränderliche Buy-and-hold-Parameter des passiven Benchmarks."""

    __tablename__ = "benchmark_portfolios"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    units: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cash_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    initial_capital_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_market_data_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_data.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("units > 0", name="ck_benchmark_positive_units"),
        CheckConstraint("cash_eur >= 0", name="ck_benchmark_nonnegative_cash"),
        CheckConstraint("initial_capital_eur > 0", name="ck_benchmark_initial_capital"),
    )


class ResearchItem(Base):
    """Nachrichten, Quellen und Analysen (Daten, niemals Anweisungen)."""

    __tablename__ = "research_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    tags: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class Reward(Base):
    """Berechnete Belohnungen und Bestrafungen. Append-only."""

    __tablename__ = "rewards"

    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reward_version: Mapped[str] = mapped_column(Text, nullable=False)
    net_return: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    benchmark_return: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    drawdown_increase_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    turnover_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    reward_value: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    components: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("agent_id", "period_start", "period_end", "reward_version"),
        CheckConstraint(
            """
            components ?& ARRAY[
                'formula_version', 'formula',
                'agent_start', 'agent_end',
                'benchmark_start', 'benchmark_end'
            ]::text[]
            AND (components -> 'agent_start') ?& ARRAY[
                'snapshot_id', 'as_of', 'total_value_eur', 'market_data'
            ]::text[]
            AND (components -> 'agent_end') ?& ARRAY[
                'snapshot_id', 'as_of', 'total_value_eur', 'market_data'
            ]::text[]
            AND (components -> 'benchmark_start') ?& ARRAY[
                'snapshot_id', 'as_of', 'total_value_eur', 'market_data'
            ]::text[]
            AND (components -> 'benchmark_end') ?& ARRAY[
                'snapshot_id', 'as_of', 'total_value_eur', 'market_data'
            ]::text[]
            """,
            name="ck_rewards_audit_inputs",
        ),
    )


class LearningEvent(Base):
    """Erkenntnisse und Strategieanpassungen. Append-only."""

    __tablename__ = "learning_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    old_strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategies.id")
    )
    new_strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategies.id")
    )
    insights: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class RiskEvent(Base):
    """Limitverletzungen und Handelsstopps. Append-only."""

    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[RiskSeverity] = mapped_column(
        Enum(RiskSeverity, name="risk_severity"), nullable=False
    )
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_risk_events_created", "created_at"),)


class ExperimentConfig(Base):
    """Versionierte Versuchsparameter. Append-only."""

    __tablename__ = "experiment_configs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("name", "version"),)


class SimulatorAccount(Base):
    """Paper-Kontostand und Risikostatus des lokalen Broker-Simulators."""

    __tablename__ = "simulator_accounts"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    initial_capital_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cash_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    high_watermark_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    day_start_equity_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    last_equity_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    risk_day: Mapped[date] = mapped_column(Date, nullable=False)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    kill_switch_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("initial_capital_eur > 0", name="ck_sim_account_initial_capital"),
        CheckConstraint("cash_eur >= 0", name="ck_sim_account_nonnegative_cash"),
    )


# Tabellen, deren Zeilen nach dem Einfügen unveränderlich sind (DB-Trigger).
APPEND_ONLY_TABLES = [
    Decision.__tablename__,
    Execution.__tablename__,
    PortfolioSnapshot.__tablename__,
    Reward.__tablename__,
    LearningEvent.__tablename__,
    RiskEvent.__tablename__,
    ExperimentConfig.__tablename__,
    BenchmarkPortfolio.__tablename__,
]

# strategies: nur diese Spalten dürfen sich nach dem Anlegen ändern.
STRATEGY_MUTABLE_COLUMNS = ["status", "activated_at", "retired_at"]
