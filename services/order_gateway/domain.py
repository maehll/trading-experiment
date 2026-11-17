"""Transactional order admission, independent risk checks, and reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading_core.models import (
    Agent,
    AgentRun,
    AgentScenario,
    AgentVariant,
    Decision,
    DecisionType,
    Execution,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PortfolioSnapshot,
    RiskEvent,
    RiskSeverity,
    RunStatus,
    SimulatorAccount,
)
from trading_core.risk import RiskConfig

from .execution import cancel_open_order, execute_order, ledger_amount
from .risk_engine import OPEN_ORDER_STATUSES, RiskEngine, cancel_open_orders
from services.portfolio.service import (
    capture_snapshot_in_transaction,
    ensure_initial_snapshot,
)

ZERO = Decimal("0")


class GatewayError(Exception):
    status_code = 400


class GatewayNotFound(GatewayError):
    status_code = 404


class GatewayConflict(GatewayError):
    status_code = 409


@dataclass(frozen=True)
class OrderCommand:
    agent_id: str
    client_order_id: str
    symbol: str
    currency: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType
    rationale: str
    isin: str | None = None
    exchange: str | None = None
    limit_price: Decimal | None = None
    time_in_force: str = "day"
    agent_run_id: UUID | None = None
    decision_id: UUID | None = None

    def fingerprint(self) -> str:
        canonical = {
            "agent_id": self.agent_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "currency": self.currency,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "order_type": self.order_type.value,
            "rationale": self.rationale,
            "isin": self.isin,
            "exchange": self.exchange,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "time_in_force": self.time_in_force,
            "agent_run_id": str(self.agent_run_id) if self.agent_run_id else None,
            "decision_id": str(self.decision_id) if self.decision_id else None,
        }
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class OrderResult:
    order: Order
    duplicate: bool = False


def _account_for_update(
    session: Session,
    agent_id: str,
    initial_capital: Decimal,
    now: datetime,
    *,
    require_initial_capital_match: bool = True,
) -> SimulatorAccount:
    session.execute(
        insert(SimulatorAccount)
        .values(
            agent_id=agent_id,
            initial_capital_eur=initial_capital,
            cash_eur=initial_capital,
            high_watermark_eur=initial_capital,
            day_start_equity_eur=initial_capital,
            last_equity_eur=initial_capital,
            risk_day=now.date(),
        )
        .on_conflict_do_nothing(index_elements=[SimulatorAccount.agent_id])
    )
    account = session.scalar(
        select(SimulatorAccount)
        .where(SimulatorAccount.agent_id == agent_id)
        .with_for_update()
    )
    if account is None:
        raise RuntimeError("Simulator-Konto konnte nicht gesperrt werden")
    if require_initial_capital_match and account.initial_capital_eur != initial_capital:
        raise GatewayConflict(
            "Startkapital weicht vom bestehenden Simulator-Konto ab; "
            "für ein neues Experiment ist ein neues Agenten-Konto erforderlich"
        )
    return account


def initialize_agent(session: Session, agent_id: str, policy: RiskConfig) -> None:
    if agent_id != "agent-a":
        raise GatewayConflict("Diese Iteration unterstützt nur Agent A")
    now = datetime.now(UTC)
    with session.begin():
        agent = session.get(Agent, agent_id)
        if agent is None:
            agent = Agent(
                id=agent_id,
                name="agent-a",
                scenario=AgentScenario.free,
                variant=AgentVariant.adaptive,
                config={"iteration": 4, "strategy_changes_enabled": False},
            )
            session.add(agent)
            session.flush()
        elif (
            agent.scenario is AgentScenario.benchmark
            or agent.variant is AgentVariant.passive
        ):
            raise GatewayConflict("Agent A ist mit einer inkompatiblen Identität angelegt")
        account = _account_for_update(
            session, agent.id, Decimal(str(policy.starting_capital_eur)), now
        )
        ensure_initial_snapshot(session, agent.id, policy, now)
        account.updated_at = now


def _reconciliation_ok(
    session: Session, account: SimulatorAccount, agent_id: str
) -> tuple[bool, str | None]:
    positions = list(
        session.scalars(
            select(Position).where(
                Position.agent_id == agent_id
            ).order_by(Position.opened_at, Position.id)
        )
    )
    orders = list(session.scalars(select(Order).where(Order.agent_id == agent_id)))
    fills = list(
        session.scalars(
            select(Execution)
            .join(Order, Order.id == Execution.order_id)
            .where(Order.agent_id == agent_id)
            .order_by(Execution.executed_at, Execution.created_at, Execution.id)
        )
    )
    cash = account.initial_capital_eur
    order_by_id = {order.id: order for order in orders}
    quantities: dict[UUID, Decimal] = {}
    cycles: dict[str, list[dict[str, Decimal | bool]]] = {}
    price_tolerance = Decimal("0.00000001") * Decimal(len(fills) + 1)
    tolerance = price_tolerance + Decimal("0.000000005") * sum(
        (fill.quantity for fill in fills if fill.market_data_id is None), ZERO
    )
    for fill in fills:
        order = order_by_id.get(fill.order_id)
        if order is None:
            return False, "Ausführung ohne zugehörige Order"
        if (
            fill.quantity <= ZERO or fill.price <= ZERO or fill.fee < ZERO
            or fill.currency != order.currency or fill.fee_currency != order.currency
        ):
            return False, "Ungültige Ausführung oder Gebührenwährung"
        if order.order_type is OrderType.limit and (
            order.limit_price is None
            or (order.side is OrderSide.buy and fill.price > order.limit_price)
            or (order.side is OrderSide.sell and fill.price < order.limit_price)
        ):
            return False, "Ausführung verletzt das Orderlimit"
        quantities[order.id] = quantities.get(order.id, ZERO) + fill.quantity
        gross = fill.quantity * fill.price
        cash = ledger_amount(cash + (
            -gross - fill.fee
            if order.side is OrderSide.buy
            else gross - fill.fee
        ))
        symbol_cycles = cycles.setdefault(order.symbol, [])
        if order.side is OrderSide.buy:
            if not symbol_cycles or symbol_cycles[-1]["closed"]:
                symbol_cycles.append({"qty": ZERO, "avg": ZERO, "realized": ZERO, "closed": False})
            state = symbol_cycles[-1]
            old_qty = state["qty"]
            state["qty"] = old_qty + fill.quantity
            state["avg"] = ledger_amount(
                (old_qty * state["avg"] + gross + fill.fee) / state["qty"]
            )
        else:
            if not symbol_cycles or symbol_cycles[-1]["closed"]:
                return False, "Verkauf ohne offene Position"
            state = symbol_cycles[-1]
            if state["qty"] < fill.quantity:
                return False, "Verkauf übersteigt Positionsmenge"
            state["realized"] = ledger_amount(
                state["realized"] + (fill.price - state["avg"]) * fill.quantity - fill.fee
            )
            state["qty"] -= fill.quantity
            state["closed"] = state["qty"] == ZERO
    if abs(cash - account.cash_eur) > tolerance:
        return False, "Cash-Abweichung zwischen Simulator-Konto und Ausführungen"
    reserved_cash = ZERO
    reserved_sells: dict[str, Decimal] = {}
    for order in orders:
        filled = quantities.get(order.id, ZERO)
        if filled > order.quantity or (
            order.status is OrderStatus.filled and filled != order.quantity
        ) or (
            order.status in (OrderStatus.submitted, OrderStatus.risk_rejected)
            and filled != ZERO
        ) or (
            order.status is OrderStatus.partially_filled
            and not ZERO < filled < order.quantity
        ) or (
            order.status is OrderStatus.cancelled and filled >= order.quantity
        ) or order.status in (OrderStatus.pending_risk, OrderStatus.risk_approved, OrderStatus.error):
            return False, f"Orderstatus und Ausführungshistorie stimmen nicht: {order.id}"
        if order.status in (OrderStatus.submitted, OrderStatus.partially_filled):
            if (
                not order.broker_order_id or order.order_type is not OrderType.limit
                or order.limit_price is None or order.limit_price <= ZERO
            ):
                return False, f"Ungültige offene Order: {order.id}"
            if order.side is OrderSide.buy:
                try:
                    reserved = Decimal(str((order.risk_check or {})["reserved_eur"]))
                except (KeyError, ValueError, InvalidOperation):
                    return False, f"Reservierung fehlt: {order.id}"
                if not reserved.is_finite() or reserved < (order.quantity - filled) * order.limit_price:
                    return False, f"Reservierung zu gering: {order.id}"
                reserved_cash += reserved
            else:
                reserved_sells[order.symbol] = reserved_sells.get(order.symbol, ZERO) + order.quantity - filled
    if reserved_cash > account.cash_eur + tolerance:
        return False, "Reserviertes Kapital übersteigt den Cash-Bestand"
    positions_by_symbol: dict[str, list[Position]] = {}
    for position in positions:
        positions_by_symbol.setdefault(position.symbol, []).append(position)
    if set(cycles) != set(positions_by_symbol):
        return False, "Positionshistorie stimmt nicht mit Ausführungen überein"
    for symbol, expected in cycles.items():
        actual = positions_by_symbol[symbol]
        if len(expected) != len(actual):
            return False, f"Positionshistorie für {symbol} unvollständig"
        for state, position in zip(expected, actual):
            if (
                state["qty"] != position.quantity
                or state["closed"] != (position.closed_at is not None)
                or abs(state["avg"] - position.avg_entry_price) > price_tolerance
                or abs(state["realized"] - position.realized_pnl) > tolerance
            ):
                return False, f"Einstand oder P&L für {symbol} stimmt nicht"
    for symbol, reserved in reserved_sells.items():
        available = sum((p.quantity for p in positions if p.symbol == symbol and p.closed_at is None), ZERO)
        if reserved > available:
            return False, f"Reservierte Verkaufsmenge übersteigt Position: {symbol}"
    snapshot = session.scalar(
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.agent_id == agent_id)
        .order_by(PortfolioSnapshot.as_of.desc())
        .limit(1)
    )
    if snapshot is not None and (not fills or snapshot.as_of >= fills[-1].executed_at):
        try:
            if snapshot.metrics is not None and not isinstance(snapshot.metrics, dict):
                raise ValueError("Snapshot-Metriken sind kein Objekt")
            data = (snapshot.metrics or {}).get("market_data", [])
            if not isinstance(data, list):
                raise ValueError("market_data ist keine Liste")
            quotes = {item["symbol"]: Decimal(str(item["price"])) for item in data}
            if any(not price.is_finite() or price <= ZERO for price in quotes.values()):
                raise ValueError("Ungültiger Bewertungskurs")
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return False, "Marktdaten im Portfolio-Snapshot sind ungültig"
        active = [p for p in positions if p.closed_at is None]
        if all(p.symbol in quotes for p in active):
            value = sum((p.quantity * quotes[p.symbol] for p in active), ZERO)
            unrealized = sum((p.quantity * (quotes[p.symbol] - p.avg_entry_price) for p in active), ZERO)
            realized = sum((p.realized_pnl for p in positions), ZERO)
            if (
                abs(snapshot.cash_eur - account.cash_eur) > tolerance
                or abs(snapshot.positions_value_eur - value) > tolerance
                or abs(snapshot.total_value_eur - account.cash_eur - value) > tolerance
                or snapshot.unrealized_pnl_eur is None
                or abs(snapshot.unrealized_pnl_eur - unrealized) > tolerance
                or snapshot.realized_pnl_eur is None
                or abs(snapshot.realized_pnl_eur - realized) > tolerance
            ):
                return False, "Portfolio-Snapshot oder P&L stimmt nicht mit Ledger überein"
        else:
            return False, "Marktdaten für P&L-Reconciliation fehlen"
    return True, None


def reconcile_agent(session: Session, agent_id: str, policy: RiskConfig) -> tuple[bool, str]:
    """Block orders unless the simulator account and immutable fill ledger agree."""
    now = datetime.now(UTC)
    with session.begin():
        agent = session.get(Agent, agent_id)
        if agent is None:
            return False, "Agent nicht gefunden"
        account = _account_for_update(
            session, agent_id, Decimal(str(policy.starting_capital_eur)), now
        )
        ok, reason = _reconciliation_ok(session, account, agent_id)
        if not ok:
            account.kill_switch_active = True
            account.kill_switch_reason = f"Reconciliation fehlgeschlagen: {reason}"
            cancel_open_orders(session, agent_id, now)
            session.add(
                RiskEvent(
                    agent_id=agent_id,
                    event_type="reconciliation_failed",
                    severity=RiskSeverity.critical,
                    details={"reason": reason},
                )
            )
    return ok, reason or "Simulator-Konto abgeglichen"


def submit_order(
    session: Session,
    command: OrderCommand,
    policy: RiskConfig,
    now: datetime | None = None,
) -> OrderResult:
    if (
        not command.quantity.is_finite() or command.quantity <= ZERO
        or command.quantity >= Decimal("1000000000000")
        or command.quantity != ledger_amount(command.quantity)
        or (command.order_type is OrderType.market and command.limit_price is not None)
        or (command.order_type is OrderType.limit and (
            command.limit_price is None or not command.limit_price.is_finite()
            or command.limit_price <= ZERO
            or command.limit_price >= Decimal("1000000000000")
            or command.limit_price != ledger_amount(command.limit_price)
        ))
    ):
        raise GatewayConflict("Ungültige Menge oder ungültiger Limitpreis")
    use_wall_clock = now is None
    now = now or datetime.now(UTC)
    fingerprint = command.fingerprint()
    with session.begin():
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": command.client_order_id},
        )
        existing = session.scalar(
            select(Order).where(Order.client_order_id == command.client_order_id)
        )
        if existing is not None:
            if (existing.risk_check or {}).get("request_fingerprint") != fingerprint:
                raise GatewayConflict("client_order_id wurde bereits anders verwendet")
            return OrderResult(existing, duplicate=True)

        agent = session.scalar(
            select(Agent).where(Agent.id == command.agent_id).with_for_update(key_share=True)
        )
        if agent is None:
            raise GatewayNotFound("Agent nicht gefunden")
        if command.agent_run_id is None or command.decision_id is None:
            raise GatewayConflict("Order benötigt einen aktiven Run und eine gespeicherte Entscheidung")
        agent_run = session.scalar(
            select(AgentRun).where(AgentRun.id == command.agent_run_id).with_for_update()
        )
        if (
            agent_run is None
            or agent_run.agent_id != agent.id
            or agent_run.status is not RunStatus.running
            or not agent.is_active
        ):
            raise GatewayConflict("Agent-Run fehlt, gehört nicht zum Agenten oder ist abgeschlossen")
        decision = session.scalar(
            select(Decision).where(Decision.id == command.decision_id).with_for_update()
        )
        expected_action = "BUY" if command.side is OrderSide.buy else "SELL"
        expected_type = DecisionType.open if command.side is OrderSide.buy else DecisionType.close
        try:
            decision_quantity = Decimal(str((decision.hypotheses or {}).get("quantity"))) if decision else None
        except InvalidOperation as exc:
            raise GatewayConflict("Entscheidung enthält eine ungültige Stückzahl") from exc
        if (
            decision is None
            or decision.agent_id != agent.id
            or decision.agent_run_id != command.agent_run_id
            or decision.decision_type is not expected_type
            or (decision.hypotheses or {}).get("action") != expected_action
            or (decision.instrument or {}).get("symbol") != command.symbol
            or decision_quantity != command.quantity
            or ((decision.instrument or {}).get("currency") is not None
                and (decision.instrument or {}).get("currency") != command.currency)
            or ((decision.instrument or {}).get("isin") is not None
                and (decision.instrument or {}).get("isin") != command.isin)
            or ((decision.instrument or {}).get("exchange") is not None
                and (decision.instrument or {}).get("exchange") != command.exchange)
        ):
            raise GatewayConflict(
                "Order stimmt nicht mit der gespeicherten Agentenentscheidung überein"
            )
        if session.scalar(select(Order.id).where(Order.decision_id == decision.id).limit(1)):
            raise GatewayConflict("Für diese Entscheidung existiert bereits eine Order")
        account = _account_for_update(
            session, agent.id, Decimal(str(policy.starting_capital_eur)), now
        )
        if use_wall_clock:
            now = datetime.now(UTC)
        ensure_initial_snapshot(session, agent.id, policy, now)
        reconciled, reconciliation_reason = _reconciliation_ok(session, account, agent.id)
        risk_engine = RiskEngine(policy)

        quote, quote_error = risk_engine.latest_quote(session, command.symbol, now)
        assessment = risk_engine.assess(
            session, agent, account, command, quote, quote_error, now
        )
        reasons = assessment.reasons
        if not reconciled:
            account.kill_switch_active = True
            account.kill_switch_reason = f"Reconciliation fehlgeschlagen: {reconciliation_reason}"
            reasons.append(account.kill_switch_reason)
            cancel_open_orders(session, agent.id, now)

        order = Order(
            client_order_id=command.client_order_id,
            decision_id=decision.id,
            agent_id=agent.id,
            symbol=command.symbol,
            isin=command.isin,
            exchange=command.exchange,
            currency=command.currency,
            side=command.side,
            quantity=command.quantity,
            order_type=command.order_type,
            limit_price=command.limit_price,
            time_in_force=command.time_in_force,
            status=OrderStatus.pending_risk,
            risk_check={
                "request_fingerprint": fingerprint,
                "reconciliation_ok": reconciled,
                "equity_eur": str(assessment.equity_eur),
                "checks": ["instrument", "market_data", "capital", "exposure", "open_orders", "kill_switch"],
            },
        )
        session.add(order)
        session.flush()

        if assessment.kill_switch_triggered:
            session.add(
                RiskEvent(
                    agent_id=agent.id,
                    order_id=order.id,
                    event_type="automatic_kill_switch",
                    severity=RiskSeverity.critical,
                    details={"reason": account.kill_switch_reason},
                )
            )
        if not reconciled:
            session.add(
                RiskEvent(
                    agent_id=agent.id,
                    order_id=order.id,
                    event_type="reconciliation_failed",
                    severity=RiskSeverity.critical,
                    details={"reason": reconciliation_reason},
                )
            )
        if reasons:
            order.status = OrderStatus.risk_rejected
            order.risk_check = {**order.risk_check, "reasons": reasons}
            session.add(
                RiskEvent(
                    agent_id=agent.id,
                    order_id=order.id,
                    event_type="order_rejected",
                    severity=RiskSeverity.warning,
                    details={"reasons": reasons},
                )
            )
        else:
            assert quote is not None
            order.status = OrderStatus.risk_approved
            order.risk_check = {
                **order.risk_check,
                "reserved_eur": str(assessment.reserved_eur),
                "risk_approved_at": now.isoformat(),
            }
            execute_order(session, account, order, quote, policy.simulator, now)
            session.flush()
            if order.status is OrderStatus.filled:
                capture_snapshot_in_transaction(
                    session, agent.id, policy, datetime.now(UTC)
                )
        session.flush()
        return OrderResult(order)


def cancel_order(
    session: Session, agent_id: str, order_id: UUID, now: datetime | None = None
) -> Order:
    now = now or datetime.now(UTC)
    with session.begin():
        account = session.scalar(
            select(SimulatorAccount)
            .where(SimulatorAccount.agent_id == agent_id)
            .with_for_update()
        )
        order = session.scalar(
            select(Order)
            .where(Order.id == order_id, Order.agent_id == agent_id)
            .with_for_update()
        )
        if account is None or order is None:
            raise GatewayNotFound("Order oder Simulator-Konto nicht gefunden")
        try:
            cancel_open_order(session, account, order, now)
        except ValueError as exc:
            raise GatewayConflict(str(exc)) from exc
        session.add(
            RiskEvent(
                agent_id=agent_id,
                order_id=order.id,
                event_type="order_cancelled",
                severity=RiskSeverity.info,
                details={"source": "authenticated gateway request"},
            )
        )
        return order


def match_open_orders(
    session: Session, agent_id: str, policy: RiskConfig, now: datetime | None = None
) -> list[dict[str, str]]:
    """Reassess and match open DAY limits once against new, current quotes."""
    use_wall_clock = now is None
    now = now or datetime.now(UTC)
    with session.begin():
        agent = session.scalar(
            select(Agent).where(Agent.id == agent_id).with_for_update(key_share=True)
        )
        account = session.scalar(
            select(SimulatorAccount)
            .where(SimulatorAccount.agent_id == agent_id)
            .with_for_update()
        )
        if agent is None or account is None:
            raise GatewayNotFound("Agent oder Simulator-Konto nicht gefunden")
        if use_wall_clock:
            now = datetime.now(UTC)
        ok, reason = _reconciliation_ok(session, account, agent_id)
        if not ok:
            account.kill_switch_active = True
            account.kill_switch_reason = f"Reconciliation fehlgeschlagen: {reason}"
            cancel_open_orders(session, agent_id, now)
            session.add(RiskEvent(
                agent_id=agent_id, event_type="reconciliation_failed",
                severity=RiskSeverity.critical, details={"reason": reason},
            ))
            return [{"status": "blocked", "reason": account.kill_switch_reason}]
        orders = list(session.scalars(
            select(Order)
            .where(
                Order.agent_id == agent_id,
                Order.status.in_((OrderStatus.submitted, OrderStatus.partially_filled)),
            )
            .order_by(Order.created_at, Order.id)
            .with_for_update()
        ))
        results: list[dict[str, str]] = []
        engine = RiskEngine(policy)
        any_filled = False
        for order in orders:
            if use_wall_clock:
                now = datetime.now(UTC)
            if order.status not in (OrderStatus.submitted, OrderStatus.partially_filled):
                continue
            if order.created_at.astimezone(UTC).date() < now.astimezone(UTC).date():
                cancel_open_order(session, account, order, now)
                results.append({"order_id": str(order.id), "status": "cancelled", "reason": "DAY abgelaufen"})
                continue
            quote, error = engine.latest_quote(session, order.symbol, now)
            if error or quote is None or quote.as_of <= order.updated_at:
                results.append({
                    "order_id": str(order.id), "status": order.status.value,
                    "reason": error or "Keine neuen Kursdaten seit Orderannahme",
                })
                continue
            filled = sum((fill.quantity for fill in session.scalars(
                select(Execution).where(Execution.order_id == order.id)
            )), ZERO)
            intent = OrderCommand(
                agent_id=agent_id, client_order_id=order.client_order_id,
                symbol=order.symbol, currency=order.currency, side=order.side,
                quantity=order.quantity - filled, order_type=order.order_type,
                limit_price=order.limit_price, rationale="Offenes Limit erneut prüfen",
                isin=order.isin, exchange=order.exchange, time_in_force=order.time_in_force,
            )
            assessment = engine.assess(
                session, agent, account, intent, quote, None, now,
                exclude_order_id=order.id,
            )
            if assessment.reasons:
                results.append({
                    "order_id": str(order.id), "status": order.status.value,
                    "reason": "; ".join(assessment.reasons),
                })
                continue
            execute_order(session, account, order, quote, policy.simulator, now)
            session.flush()
            if order.status is OrderStatus.filled:
                any_filled = True
            results.append({"order_id": str(order.id), "status": order.status.value})
        if any_filled:
            capture_snapshot_in_transaction(session, agent_id, policy, now)
        session.flush()
        return results


def activate_kill_switch(
    session: Session, agent_id: str, policy: RiskConfig, reason: str
) -> None:
    now = datetime.now(UTC)
    with session.begin():
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise GatewayNotFound("Agent nicht gefunden")
        account = _account_for_update(
            session,
            agent.id,
            Decimal(str(policy.starting_capital_eur)),
            now,
            require_initial_capital_match=False,
        )
        account.kill_switch_active = True
        account.kill_switch_reason = reason
        account.updated_at = now
        cancel_open_orders(session, agent_id, now)
        session.add(
            RiskEvent(
                agent_id=agent_id,
                event_type="manual_kill_switch",
                severity=RiskSeverity.critical,
                details={"reason": reason},
            )
        )
