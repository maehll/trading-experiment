"""One-shot autonomous analysis with no DB, risk, or simulator imports."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Protocol

from pydantic import ValidationError

from trading_core.settings import AgentRuntimeSettings

from .openrouter import LLMCompletion, OpenRouterError
from .proposal import DecisionProposal

SOURCE_PATTERN = re.compile(r"^(market_data|research_item):[0-9a-fA-F-]{36}$")


class DataClient(Protocol):
    def get_context(self, agent_id: str) -> dict: ...


class GatewayClient(Protocol):
    def start_run(self, agent_id: str, model: str, trigger: str) -> dict: ...

    def finish_run(self, run_id: str, payload: dict) -> dict: ...

    def record_decision(self, payload: dict) -> dict: ...

    def submit_order(self, payload: dict) -> dict: ...


class LLMClient(Protocol):
    def complete(
        self, decision_context: dict[str, object], *, usd_per_eur: Decimal
    ) -> LLMCompletion: ...


class AgentRunFailure(RuntimeError):
    """A run failed closed before an actionable order could be sent."""


def _decision_context(data: dict) -> dict[str, object]:
    return {
        "captured_at": data["captured_at"],
        "instruments": data["instruments"],
        "universe_truncated": data["universe_truncated"],
        "research": data["research"],
        "portfolio": data["portfolio"],
    }


def _available_sources(context: dict[str, object]) -> set[str]:
    sources = {
        f"market_data:{instrument['market_data_id']}"
        for instrument in context["instruments"]
    }
    sources.update(
        f"research_item:{item['research_item_id']}"
        for item in context["research"]
    )
    return sources


def _validate_proposal(
    proposal: DecisionProposal, context: dict[str, object]
) -> None:
    available_sources = _available_sources(context)
    if any(not SOURCE_PATTERN.fullmatch(source_id) for source_id in proposal.source_ids):
        raise AgentRunFailure("LLM lieferte eine ungültige Quellenreferenz")
    if not set(proposal.source_ids).issubset(available_sources):
        raise AgentRunFailure("LLM nannte Quellen, die nicht im Datenkontext enthalten sind")
    instruments = {instrument["symbol"] for instrument in context["instruments"]}
    if proposal.symbol is not None and proposal.symbol not in instruments:
        raise AgentRunFailure("LLM wählte ein Instrument außerhalb der Datenquelle")
    if proposal.action in {"BUY", "SELL"}:
        instrument = next(
            (
                item
                for item in context["instruments"]
                if item["symbol"] == proposal.symbol
            ),
            None,
        )
        if instrument is None or instrument["currency"] != "EUR":
            raise AgentRunFailure(
                "LLM wählte ein Instrument ohne aktuelle EUR-Marktdaten"
            )
        if proposal.quantity is None:
            raise AgentRunFailure("LLM-Ordervorschlag enthält keine Stückzahl")
        if f"market_data:{instrument['market_data_id']}" not in proposal.source_ids:
            raise AgentRunFailure("LLM muss den Kurs des Orderinstruments zitieren")


def _finish_payload(
    completion: LLMCompletion | None,
    *,
    status: str,
    error: str | None = None,
) -> dict:
    return {
        "status": status,
        "model": completion.model if completion else None,
        "provider_request_id": completion.provider_request_id if completion else None,
        "input_tokens": completion.input_tokens if completion else None,
        "output_tokens": completion.output_tokens if completion else None,
        "cost_usd": str(completion.cost_usd) if completion else None,
        "error": error,
    }


def _reject_known_secrets(content: str, settings: AgentRuntimeSettings) -> None:
    for secret in (
        settings.openrouter_api_key,
        settings.data_api_key,
        settings.gateway_api_key,
    ):
        value = secret.get_secret_value()
        if value and (value in content or json.dumps(value, ensure_ascii=False)[1:-1] in content):
            raise AgentRunFailure("Daten oder Modellantwort enthalten ein Prozessgeheimnis")


def run_once(
    settings: AgentRuntimeSettings,
    data_client: DataClient,
    gateway: GatewayClient,
    llm: LLMClient,
    *,
    trigger: str = "manual",
) -> dict[str, object]:
    run = gateway.start_run(settings.agent_id, settings.model, trigger)
    run_id = run["run_id"]
    completion: LLMCompletion | None = None
    try:
        raw_context = data_client.get_context(settings.agent_id)
        context = _decision_context(raw_context)
        serialized_context = json.dumps(
            context, ensure_ascii=False, separators=(",", ":")
        )
        if len(serialized_context.encode("utf-8")) > 100_000:
            raise AgentRunFailure("Datenkontext überschreitet das sichere 100-KB-Limit")
        _reject_known_secrets(serialized_context, settings)

        if not context["instruments"]:
            decision = {
                "agent_id": settings.agent_id,
                "agent_run_id": run_id,
                "action": "NO_TRADE",
                "symbol": None,
                "quantity": None,
                "rationale": "Keine aktuellen, validen EUR-Aktien- oder ETF-Kurse verfügbar.",
                "investment_horizon_days": 1,
                "confidence": 1.0,
                "source_ids": [],
                "market_context": {
                    "data_context": context,
                    "guard": "empty_market_universe",
                },
            }
            decision_response = gateway.record_decision(decision)
            gateway.finish_run(
                run_id,
                {
                    "status": "succeeded",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": "0",
                    "error": None,
                },
            )
            return {
                "run_id": run_id,
                "decision_id": decision_response["decision_id"],
                "action": "NO_TRADE",
                "order": None,
            }

        fx = raw_context.get("budget_fx")
        if not isinstance(fx, dict):
            raise AgentRunFailure(
                "Zeitgerechter EURUSD-Kurs fehlt; Budget kann nicht sicher geprüft werden"
            )
        usd_per_eur = Decimal(str(fx["usd_per_eur"]))
        if usd_per_eur <= 0:
            raise AgentRunFailure("Ungültiger EURUSD-Budgetkurs")

        completion = llm.complete(context, usd_per_eur=usd_per_eur)
        _reject_known_secrets(completion.content, settings)
        proposal = DecisionProposal.model_validate_json(completion.content)
        _validate_proposal(proposal, context)

        decision_response = gateway.record_decision(
            {
                "agent_id": settings.agent_id,
                "agent_run_id": run_id,
                "action": proposal.action,
                "symbol": proposal.symbol,
                "quantity": str(proposal.quantity) if proposal.quantity else None,
                "rationale": proposal.rationale,
                "investment_horizon_days": proposal.investment_horizon_days,
                "confidence": proposal.confidence,
                "source_ids": proposal.source_ids,
                "market_context": context,
            }
        )
        decision_id = decision_response["decision_id"]

        order_result = None
        if proposal.action in {"BUY", "SELL"}:
            if not context["portfolio"]["complete"]:
                raise AgentRunFailure("BUY/SELL ohne vollständiges Portfolio nicht ausführbar")
            instrument = next(
                item for item in context["instruments"] if item["symbol"] == proposal.symbol
            )
            order_result = gateway.submit_order(
                {
                    "agent_id": settings.agent_id,
                    "agent_run_id": run_id,
                    "decision_id": decision_id,
                    "client_order_id": f"agent-a-{run_id}",
                    "symbol": proposal.symbol,
                    "currency": "EUR",
                    "isin": instrument.get("isin"),
                    "exchange": instrument.get("exchange"),
                    "side": proposal.action.lower(),
                    "quantity": str(proposal.quantity),
                    "order_type": "market",
                    "time_in_force": "day",
                    "rationale": proposal.rationale,
                }
            )

        gateway.finish_run(
            run_id, _finish_payload(completion, status="succeeded")
        )
        return {
            "run_id": run_id,
            "decision_id": decision_id,
            "action": proposal.action,
            "order": order_result,
        }
    except Exception as exc:
        try:
            gateway.finish_run(
                run_id,
                _finish_payload(
                    completion,
                    status="failed",
                    error=f"{type(exc).__name__}: Agent-Run fehlgeschlagen",
                ),
            )
        except Exception as finish_error:
            raise AgentRunFailure(
                "Agent-Run fehlgeschlagen und Gateway konnte den Abschluss nicht protokollieren; Orderstatus prüfen"
            ) from finish_error
        if isinstance(exc, (OpenRouterError, AgentRunFailure)):
            raise AgentRunFailure(str(exc)) from exc
        if isinstance(exc, ValidationError):
            raise AgentRunFailure("Ungültige Modellantwort; keine Order gesendet") from exc
        raise AgentRunFailure(
            "Agent-Run fehlgeschlagen; Gateway- und Orderstatus prüfen"
        ) from exc
