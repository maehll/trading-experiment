"""Versioned reward calculation policy."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class RewardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["v1", "v2"]
    lambda_benchmark: float = Field(ge=0)
    lambda_drawdown: float = Field(ge=0)
    lambda_turnover: float = Field(ge=0)
    usd_eur_symbol: str = Field(default="EURUSD", min_length=1, max_length=32)
    usd_eur_max_age_seconds: int = Field(default=259200, gt=0, le=604800)


def load_reward_config(path: Path | None = None) -> RewardConfig:
    config_path = path or (
        Path(__file__).resolve().parents[2] / "config" / "reward.yaml"
    )
    with config_path.open("r", encoding="utf-8") as file:
        document = yaml.safe_load(file)
    if not isinstance(document, dict) or set(document) != {"reward"}:
        raise ValueError("Reward-Konfiguration muss genau einen 'reward'-Abschnitt enthalten")
    return RewardConfig.model_validate(document["reward"])
