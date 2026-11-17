"""Independent fail-closed order risk evaluation for the paper gateway."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_core.models import (
    Agent,
    DataQuality,
    Execution,
    MarketData,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    SimulatorAccount,
)
from trading_core.risk import RiskConfig
from .execution import touch_price

ZERO = Decimal("0")
OPEN_ORDER_STATUSES = (
    OrderStatus.pending_risk,
    OrderStatus.risk_approved,
    OrderStatus.submitted,
    OrderStatus.partially_filled,
)


class OrderIntent(Protocol):
    agent_id: str
    symbol: str
    isin: str | None
    exchange: str | None
    currency: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    time_in_force: str


@dataclass(frozen=True)
class RiskAssessment:
    reasons: list[str]
    equity_eur: Decimal
    reserved_eur: Decimal
    kill_switch_triggered: bool = False


class RiskEngine:
    def __init__(self, policy: RiskConfig):
        self.policy = policy

    def latest_quote(
        self, session: Session, symbol: str, now: datetime
    ) -> tuple[MarketData | None, str | None]:
        quote = session.scalar(
            select(MarketData)
            .where(
                MarketData.symbol == symbol,
                MarketData.as_of <= now,
                MarketData.fetched_at <= now,
            )
            .order_by(MarketData.as_of.desc(), MarketData.fetched_at.desc())
            .limit(1)
        )
        if quote is None:
            return None, "Keine Marktdaten für das Instrument"
        if quote.quality is not DataQuality.ok:
            return None, "Marktdaten sind als stale oder suspect markiert"
        age = now - quote.as_of
        if age < timedelta(0):
            return None, "Marktdaten liegen unzulässig in der Zukunft"
        if age > timedelta(seconds=self.policy.market_data_max_age_seconds):
            return None, "Marktdaten sind veraltet"
        if quote.price <= ZERO or quote.currency != "EUR":
            return None, "Ungültiger Kurs oder nicht unterstützte Währung (nur EUR lokal)"
        if quote.bid is not None and quote.bid <= ZERO:
            return None, "Ungültiger Geldkurs"
        if quote.ask is not None and quote.ask <= ZERO:
            return None, "Ungültiger Briefkurs"
        if quote.bid is not None and quote.ask is not None and quote.bid > quote.ask:
            return None, "Geld-/Briefkurse sind widersprüchlich"
        for key in ("isin", "exchange"):
            identity = MarketData.meta[key].astext
            known = list(session.scalars(
                select(identity).where(
                    MarketData.symbol == symbol,
                    MarketData.as_of <= now,
                    MarketData.fetched_at <= now,
                    identity.is_not(None),
                    identity != "",
                ).distinct().limit(2)
            ))
            if len(known) > 1:
                return None, f"Mehrdeutige Instrumentenidentität ({key}) für {symbol}"
        instrument_type = (quote.meta or {}).get("instrument_type")
        if instrument_type not in {"stock", "etf"}:
            return None, "Instrumententyp nicht identifiziert oder nicht freigegeben"
        return quote, None

    def mark_positions(
        self, session: Session, agent_id: str, now: datetime
    ) -> tuple[list[Position], Decimal, str | None]:
        positions = list(
            session.scalars(
                select(Position)
                .where(Position.agent_id == agent_id, Position.closed_at.is_(None))
                .with_for_update()
            )
        )
        value = ZERO
        for position in positions:
            quote, error = self.latest_quote(session, position.symbol, now)
            if error:
                return positions, ZERO, f"Bestehende Position {position.symbol}: {error}"
            if quote is None or quote.currency != position.currency:
                return positions, ZERO, f"Währungskonflikt für {position.symbol}"
            value += position.quantity * quote.price
        return positions, value, None

    def assess(
        self,
        session: Session,
        agent: Agent,
        account: SimulatorAccount,
        intent: OrderIntent,
        quote: MarketData | None,
        quote_error: str | None,
        now: datetime,
        *,
        exclude_order_id: UUID | None = None,
    ) -> RiskAssessment:
        reasons: list[str] = []
        if not agent.is_active:
            reasons.append("Agent ist deaktiviert")
        if agent.scenario.value == "benchmark":
            reasons.append("Benchmark-Agenten dürfen keine Einzelorders einreichen")
        if intent.currency != "EUR":
            reasons.append("Lokale Paper-Ausführung unterstützt derzeit nur EUR")
        if intent.time_in_force.lower() != "day":
            reasons.append("Nur DAY-Orders sind lokal zulässig")
        if quote_error:
            reasons.append(quote_error)
        if quote is None:
            return RiskAssessment(reasons, ZERO, ZERO)
        if quote.currency != intent.currency:
            reasons.append("Order- und Marktdatenwährung stimmen nicht überein")
        quote_meta = quote.meta or {}
        if intent.isin is not None and quote_meta.get("isin") != intent.isin:
            reasons.append("ISIN stimmt nicht mit der verbindlichen Instrumenten-ID überein")
        if intent.exchange is not None and quote_meta.get("exchange") != intent.exchange:
            reasons.append("Börsenplatz stimmt nicht mit den Marktdaten überein")

        positions, positions_value, mark_error = self.mark_positions(
            session, agent.id, now
        )
        if mark_error:
            reasons.append(mark_error)
        equity = account.cash_eur + positions_value
        if equity <= ZERO:
            reasons.append("Portfolio-Eigenkapital ist nicht positiv")
            return RiskAssessment(reasons, equity, ZERO)

        if not mark_error:
            if account.risk_day != now.date():
                account.risk_day = now.date()
                account.day_start_equity_eur = max(account.last_equity_eur, equity)
            account.last_equity_eur = equity
        account.high_watermark_eur = max(account.high_watermark_eur, equity)
        daily_loss_pct = (
            max(
                ZERO,
                (account.day_start_equity_eur - equity)
                / account.day_start_equity_eur
                * 100,
            )
            if account.day_start_equity_eur > ZERO
            else Decimal("100")
        )
        drawdown_pct = (
            max(
                ZERO,
                (account.high_watermark_eur - equity)
                / account.high_watermark_eur
                * 100,
            )
            if account.high_watermark_eur > ZERO
            else Decimal("100")
        )
        limits = self.policy.defaults
        was_kill_switch_active = account.kill_switch_active
        if (
            self.policy.kill_switch.enabled
            and daily_loss_pct >= Decimal(str(limits.max_daily_loss_pct))
        ):
            account.kill_switch_active = True
            account.kill_switch_reason = "Maximale Tagesverlustgrenze erreicht"
        if (
            self.policy.kill_switch.enabled
            and drawdown_pct >= Decimal(str(limits.max_drawdown_pct))
        ):
            account.kill_switch_active = True
            account.kill_switch_reason = "Maximaler Drawdown erreicht"
        if account.kill_switch_active:
            reasons.append(account.kill_switch_reason or "Kill-Switch ist aktiv")
            cancel_open_orders(session, agent.id, now)
        kill_switch_triggered = not was_kill_switch_active and account.kill_switch_active

        reference_price = touch_price(intent.side, quote, self.policy.simulator)
        agent_policy = self.policy.for_agent(agent.id)
        slippage = Decimal(str(self.policy.simulator.slippage_bps)) / Decimal("10000")
        if intent.order_type is OrderType.limit:
            assert intent.limit_price is not None
            risk_price = (
                max(reference_price, intent.limit_price)
                if intent.side is OrderSide.buy
                else min(reference_price, intent.limit_price)
            )
        else:
            risk_price = reference_price * (
                Decimal("1") + slippage
                if intent.side is OrderSide.buy
                else Decimal("1") - slippage
            )
        notional = risk_price * intent.quantity
        fee = notional * Decimal(str(self.policy.simulator.fee_bps)) / Decimal("10000")
        order_cost = notional + fee

        existing_symbol_qty = sum(
            (position.quantity for position in positions if position.symbol == intent.symbol),
            ZERO,
        )
        open_buys = list(
            session.scalars(
                select(Order).where(
                    Order.agent_id == agent.id,
                    Order.status.in_(OPEN_ORDER_STATUSES),
                    Order.side == OrderSide.buy,
                )
            )
        )
        open_sells = list(
            session.scalars(
                select(Order).where(
                    Order.agent_id == agent.id,
                    Order.status.in_(OPEN_ORDER_STATUSES),
                    Order.side == OrderSide.sell,
                )
            )
        )
        open_buys = [order for order in open_buys if order.id != exclude_order_id]
        open_sells = [order for order in open_sells if order.id != exclude_order_id]
        reserved_cash = sum(
            (
                Decimal(str((order.risk_check or {}).get("reserved_eur", "0")))
                for order in open_buys
            ),
            ZERO,
        )
        reserved_position_qty = ZERO
        for order in open_sells:
            if order.symbol == intent.symbol:
                filled = session.scalar(
                    select(func.coalesce(func.sum(Execution.quantity), 0)).where(
                        Execution.order_id == order.id
                    )
                )
                reserved_position_qty += order.quantity - filled

        if intent.side is OrderSide.sell:
            available_qty = existing_symbol_qty - reserved_position_qty
            if intent.quantity > available_qty:
                reasons.append(
                    "Verkauf überschreitet verfügbare Long-Position; "
                    "Leerverkäufe sind im Simulator gesperrt"
                )
        else:
            projected_cash = account.cash_eur - reserved_cash - order_cost
            min_cash = equity * Decimal(str(limits.min_cash_pct)) / 100
            if projected_cash < min_cash:
                reasons.append("Mindest-Cashreserve oder verfügbares Kapital verletzt")

        max_order = equity * Decimal(str(limits.max_order_pct)) / 100
        if order_cost > max_order:
            reasons.append("Maximale Einzelordergröße überschritten")
        if intent.side is OrderSide.buy:
            symbol_value = existing_symbol_qty * quote.price
            reserved_symbol_value = sum(
                (
                    Decimal(str((order.risk_check or {}).get("reserved_eur", "0")))
                    for order in open_buys
                    if order.symbol == intent.symbol
                ),
                ZERO,
            )
            max_position = equity * Decimal(str(limits.max_position_pct)) / 100
            if symbol_value + reserved_symbol_value + order_cost > max_position:
                reasons.append("Maximale Positionsgröße überschritten")
            gross_open_orders = sum(
                (
                    Decimal(str((order.risk_check or {}).get("reserved_eur", "0")))
                    for order in open_buys
                ),
                ZERO,
            )
            gross = positions_value + gross_open_orders + notional
            if gross > equity * Decimal(
                str(agent_policy.leverage.max_gross_exposure_multiple)
            ):
                reasons.append("Maximales Brutto-Exposure überschritten")

        return RiskAssessment(reasons, equity, order_cost, kill_switch_triggered)


def cancel_open_orders(session: Session, agent_id: str, now: datetime) -> None:
    for order in session.scalars(
        select(Order)
        .where(
            Order.agent_id == agent_id,
            Order.status.in_(OPEN_ORDER_STATUSES),
        )
        .with_for_update()
    ):
        if order.status in (OrderStatus.submitted, OrderStatus.partially_filled):
            order.status = OrderStatus.cancelled
            order.risk_check = {**(order.risk_check or {}), "reserved_eur": "0"}
            order.updated_at = now
