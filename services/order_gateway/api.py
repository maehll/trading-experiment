"""Authenticated local API for simulator-only order submission."""

from __future__ import annotations

import secrets
import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from trading_core.models import Execution, OrderSide, OrderType, RunStatus
from trading_core.risk import RiskConfig

from .domain import (
    GatewayError,
    OrderCommand,
    activate_kill_switch,
    cancel_order,
    initialize_agent,
    match_open_orders,
    submit_order,
)
from .agent_runs import (
    AgentRunError,
    record_agent_decision,
    finish_agent_run,
    start_agent_run,
)


class AgentDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=80)
    agent_run_id: UUID
    action: Literal["BUY", "SELL", "HOLD", "NO_TRADE"]
    symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=8)
    rationale: str = Field(min_length=1, max_length=2000)
    investment_horizon_days: int = Field(ge=1, le=3650)
    confidence: float = Field(ge=0, le=1)
    source_ids: list[str] = Field(max_length=40)
    market_context: dict[str, object]

    @model_validator(mode="after")
    def limit_context_size(self) -> "AgentDecisionRequest":
        serialized = json.dumps(
            self.market_context, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(serialized) > 100_000:
            raise ValueError("Entscheidungskontext überschreitet 100 KB")
        if self.action in {"BUY", "SELL"} and (
            self.symbol is None or self.quantity is None
        ):
            raise ValueError("BUY und SELL erfordern Instrument und Stückzahl")
        if self.action in {"HOLD", "NO_TRADE"} and self.quantity is not None:
            raise ValueError("HOLD und NO_TRADE dürfen keine Stückzahl enthalten")
        return self


class AgentRunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=128)
    trigger: str = Field(default="scheduled", min_length=1, max_length=32)


class AgentRunFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    model: str | None = Field(default=None, max_length=128)
    provider_request_id: str | None = Field(default=None, max_length=128)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)
    error: str | None = Field(default=None, max_length=1000)


class OrderSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=80)
    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
    isin: str | None = Field(default=None, pattern=r"^[A-Z0-9]{12}$")
    exchange: str | None = Field(default=None, max_length=32)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    side: OrderSide
    quantity: Decimal = Field(gt=0, max_digits=20, decimal_places=8)
    order_type: OrderType
    limit_price: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=8)
    time_in_force: str = Field(default="day", pattern=r"^[A-Za-z]{1,8}$")
    rationale: str = Field(min_length=1, max_length=2000)
    agent_run_id: UUID | None = None
    decision_id: UUID | None = None

    @model_validator(mode="after")
    def validate_order_type(self) -> "OrderSubmission":
        if self.order_type is OrderType.limit and self.limit_price is None:
            raise ValueError("limit_price ist für Limit-Orders erforderlich")
        if self.order_type is OrderType.market and self.limit_price is not None:
            raise ValueError("limit_price ist für Market-Orders nicht zulässig")
        return self


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


def create_app(
    session_factory: sessionmaker[Session],
    policy: RiskConfig,
    api_key: str,
    agent_api_key: str | None = None,
) -> FastAPI:
    if (
        agent_api_key
        and api_key
        and secrets.compare_digest(agent_api_key, api_key)
    ):
        raise ValueError("Admin- und Agent-Gateway-Schlüssel müssen verschieden sein")
    app = FastAPI(
        title="Trading Experiment Order Gateway",
        version="0.1.0",
        description="Local paper trading only; no live-broker adapter is configured.",
    )

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if not api_key or len(api_key) < 32:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Gateway nicht konfiguriert: TRADING_GATEWAY_API_KEY muss mindestens 32 Zeichen haben",
            )
        if x_api_key is None or not secrets.compare_digest(x_api_key, api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Ungültiger oder fehlender Gateway-Schlüssel",
            )

    def require_order_key(
        x_api_key: str | None = Header(default=None),
    ) -> str:
        if not api_key or len(api_key) < 32:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Gateway nicht konfiguriert",
            )
        if x_api_key is not None and secrets.compare_digest(x_api_key, api_key):
            return "admin"
        if agent_api_key and len(agent_api_key) >= 32:
            if x_api_key is not None and secrets.compare_digest(
                x_api_key, agent_api_key
            ):
                return "agent"
        elif not agent_api_key and x_api_key is not None and secrets.compare_digest(
            x_api_key, api_key
        ):
            return "agent"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiger oder fehlender Gateway-Schlüssel",
        )

    def require_agent_key(x_api_key: str | None = Header(default=None)) -> None:
        if not agent_api_key or len(agent_api_key) < 32:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent-Gateway-Schlüssel ist nicht konfiguriert",
            )
        if x_api_key is None or not secrets.compare_digest(x_api_key, agent_api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Ungültiger Agent-Gateway-Schlüssel",
            )

    @app.get("/health")
    def health(session: Session = Depends(get_session)) -> dict[str, str]:
        session.execute(text("SELECT 1"))
        return {"status": "ok", "execution_mode": "paper"}

    @app.post("/orders")
    def create_order(
        request: OrderSubmission,
        principal: str = Depends(require_order_key),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        if request.agent_run_id is None or request.decision_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Agentenorders benötigen einen aktiven Run und eine gespeicherte Entscheidung",
            )
        if principal == "agent" and request.agent_id != "agent-a":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agent-Schlüssel ist ausschließlich für Agent A gültig",
            )
        command = OrderCommand(
            agent_id=request.agent_id,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            isin=request.isin,
            exchange=request.exchange,
            currency=request.currency,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            limit_price=request.limit_price,
            time_in_force=request.time_in_force.lower(),
            rationale=request.rationale,
            agent_run_id=request.agent_run_id,
            decision_id=request.decision_id,
        )
        try:
            result = submit_order(session, command, policy)
        except GatewayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        fill = session.scalar(
            select(Execution)
            .where(Execution.order_id == result.order.id)
            .order_by(Execution.executed_at)
            .limit(1)
        )
        risk = result.order.risk_check or {}
        return {
            "order_id": str(result.order.id),
            "client_order_id": result.order.client_order_id,
            "status": result.order.status.value,
            "duplicate": result.duplicate,
            "fill_price": str(fill.price) if fill else None,
            "fee": str(fill.fee) if fill else None,
            "risk_reasons": risk.get("reasons", []),
        }

    @app.post("/agent-runs", status_code=status.HTTP_201_CREATED)
    def create_agent_run(
        request: AgentRunStart,
        _: None = Depends(require_agent_key),
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        try:
            run = start_agent_run(
                session, request.agent_id, request.model, request.trigger
            )
        except AgentRunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": str(run.id), "status": run.status.value}

    @app.post("/agent-runs/{run_id}/finish")
    def complete_agent_run(
        run_id: UUID,
        request: AgentRunFinish,
        _: None = Depends(require_agent_key),
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        try:
            run = finish_agent_run(
                session,
                run_id,
                status=request.status,
                model=request.model,
                provider_request_id=request.provider_request_id,
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
                cost_usd=request.cost_usd,
                error=request.error,
            )
        except AgentRunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": str(run.id), "status": run.status.value}

    @app.post("/decisions", status_code=status.HTTP_201_CREATED)
    def create_agent_decision(
        request: AgentDecisionRequest,
        _: None = Depends(require_agent_key),
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        try:
            decision = record_agent_decision(
                session,
                agent_id=request.agent_id,
                run_id=request.agent_run_id,
                action=request.action,
                symbol=request.symbol,
                quantity=request.quantity,
                rationale=request.rationale,
                investment_horizon_days=request.investment_horizon_days,
                confidence=Decimal(str(request.confidence)),
                source_ids=request.source_ids,
                market_context={
                    "data_context": request.market_context,
                    "cited_source_ids": request.source_ids,
                },
            )
        except AgentRunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "decision_id": str(decision.id),
            "action": request.action,
        }

    @app.post("/agents/{agent_id}/orders/{order_id}/cancel")
    def cancel(
        agent_id: str,
        order_id: UUID,
        _: None = Depends(require_api_key),
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        try:
            order = cancel_order(session, agent_id, order_id)
        except GatewayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"order_id": str(order.id), "status": order.status.value}

    @app.post("/admin/agents/{agent_id}/orders/match")
    def match(
        agent_id: str,
        _: None = Depends(require_api_key),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        try:
            results = match_open_orders(session, agent_id, policy)
        except GatewayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"agent_id": agent_id, "orders": results}

    @app.post("/admin/agents/{agent_id}/initialize")
    def initialize_agent_portfolio(
        agent_id: str,
        _: None = Depends(require_api_key),
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        try:
            initialize_agent(session, agent_id, policy)
        except GatewayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"agent_id": agent_id, "status": "initialized", "execution_mode": "paper"}

    @app.post("/agents/{agent_id}/kill-switch")
    def kill_switch(
        agent_id: str,
        request: KillSwitchRequest,
        _: None = Depends(require_api_key),
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        try:
            activate_kill_switch(session, agent_id, policy, request.reason)
        except GatewayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"agent_id": agent_id, "kill_switch": "active"}

    return app
