"""Deterministic, local-only paper broker simulator."""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_core.models import (
    Execution,
    MarketData,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    SimulatorAccount,
)
from trading_core.risk import SimulatorPolicy

ZERO = Decimal("0")
BPS = Decimal("10000")
MONEY_PRECISION = Decimal("0.00000001")


def ledger_amount(value: Decimal) -> Decimal:
    return value.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)


def touch_price(side: OrderSide, quote: MarketData, policy: SimulatorPolicy) -> Decimal:
    buy = side is OrderSide.buy
    actual = quote.ask if buy else quote.bid
    if actual is not None:
        return actual
    half_spread = Decimal(str(policy.spread_bps)) / (BPS * 2)
    return quote.price * (Decimal("1") + half_spread if buy else Decimal("1") - half_spread)


def execution_price(order: Order, quote: MarketData, policy: SimulatorPolicy) -> Decimal | None:
    """Return a simulated fill price; non-marketable limit orders stay open."""
    buy = order.side is OrderSide.buy
    touch = touch_price(order.side, quote, policy)
    slippage = Decimal(str(policy.slippage_bps)) / BPS
    price = touch * (Decimal("1") + slippage if buy else Decimal("1") - slippage)
    if order.order_type is OrderType.limit:
        if buy and order.limit_price < price:
            return None
        if not buy and order.limit_price > price:
            return None
    return price


def execute_order(
    session: Session,
    account: SimulatorAccount,
    order: Order,
    quote: MarketData,
    policy: SimulatorPolicy,
    now: datetime,
) -> None:
    """Fill immediately when executable, updating only the simulator ledger."""
    fill_price = execution_price(order, quote, policy)
    if fill_price is None:
        if order.status is not OrderStatus.partially_filled:
            order.status = OrderStatus.submitted
        order.broker_order_id = f"sim-{order.id}"
        order.updated_at = now
        return

    filled = session.scalar(
        select(func.coalesce(func.sum(Execution.quantity), 0)).where(
            Execution.order_id == order.id
        )
    )
    quantity = order.quantity - filled
    if quantity <= ZERO:
        raise RuntimeError("Order hat keine verbleibende ausführbare Menge")
    fill_price = ledger_amount(fill_price)
    notional = fill_price * quantity
    fee = ledger_amount(notional * Decimal(str(policy.fee_bps)) / BPS)
    position = session.scalar(
        select(Position)
        .where(
            Position.agent_id == order.agent_id,
            Position.symbol == order.symbol,
            Position.closed_at.is_(None),
        )
        .with_for_update()
    )

    if order.side is OrderSide.buy:
        account.cash_eur = ledger_amount(account.cash_eur - notional - fee)
        if position is None:
            position = Position(
                agent_id=order.agent_id,
                symbol=order.symbol,
                currency=order.currency,
                quantity=quantity,
                avg_entry_price=ledger_amount((notional + fee) / quantity),
                realized_pnl=ZERO,
                opened_at=now,
                updated_at=now,
            )
            session.add(position)
        else:
            total_cost = position.quantity * position.avg_entry_price + notional + fee
            position.quantity += quantity
            position.avg_entry_price = ledger_amount(total_cost / position.quantity)
            position.updated_at = now
    else:
        if position is None or position.quantity < quantity:
            raise RuntimeError("Risk Engine ließ einen ungedeckten Verkauf zu")
        account.cash_eur = ledger_amount(account.cash_eur + notional - fee)
        position.realized_pnl = ledger_amount(
            position.realized_pnl + (fill_price - position.avg_entry_price) * quantity - fee
        )
        position.quantity -= quantity
        position.updated_at = now
        if position.quantity == ZERO:
            position.closed_at = now

    session.add(
        Execution(
            order_id=order.id,
            broker_execution_id=f"sim-exec-{uuid4()}",
            executed_at=now,
            quantity=quantity,
            price=fill_price,
            currency=order.currency,
            fee=fee,
            fee_currency=order.currency,
            market_data_id=quote.id,
            simulation_details={
                "quote_as_of": quote.as_of.isoformat(),
                "quote_fetched_at": quote.fetched_at.isoformat(),
                "mid": str(quote.price),
                "bid": str(quote.bid) if quote.bid is not None else None,
                "ask": str(quote.ask) if quote.ask is not None else None,
                "spread_bps_if_missing": str(policy.spread_bps),
                "slippage_bps": str(policy.slippage_bps),
                "fee_bps": str(policy.fee_bps),
            },
        )
    )
    order.status = OrderStatus.filled
    order.risk_check = {**(order.risk_check or {}), "reserved_eur": "0"}
    order.broker_order_id = f"sim-{order.id}"
    order.updated_at = now
    account.updated_at = now


def cancel_open_order(
    session: Session, account: SimulatorAccount, order: Order, now: datetime
) -> None:
    if order.status not in (OrderStatus.submitted, OrderStatus.partially_filled):
        raise ValueError("Nur offene Simulator-Orders können storniert werden")
    order.status = OrderStatus.cancelled
    order.risk_check = {**(order.risk_check or {}), "reserved_eur": "0"}
    order.updated_at = now
    account.updated_at = now
