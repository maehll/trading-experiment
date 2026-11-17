"""Validated, tool-free decision contract for Agent A."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DecisionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["BUY", "SELL", "HOLD", "NO_TRADE"]
    symbol: str | None = Field(
        default=None, pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$"
    )
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=8)
    rationale: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(min_length=1, max_length=40)
    investment_horizon_days: int = Field(ge=1, le=3650)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_action(self) -> "DecisionProposal":
        if self.action in {"BUY", "SELL"} and (
            self.symbol is None or self.quantity is None
        ):
            raise ValueError("BUY und SELL erfordern Instrument und Stückzahl")
        if self.action in {"HOLD", "NO_TRADE"} and self.quantity is not None:
            raise ValueError("HOLD und NO_TRADE dürfen keine Stückzahl enthalten")
        return self


DECISION_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": ["BUY", "SELL", "HOLD", "NO_TRADE"],
        },
        "symbol": {
            "type": ["string", "null"],
            "pattern": "^[A-Z0-9][A-Z0-9.-]{0,19}$",
        },
        "quantity": {"type": ["string", "null"]},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 2000},
        "source_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 40,
        },
        "investment_horizon_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3650,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "action",
        "symbol",
        "quantity",
        "rationale",
        "source_ids",
        "investment_horizon_days",
        "confidence",
    ],
}
