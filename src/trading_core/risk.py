"""Strict, administrator-owned risk configuration loaded outside agent data."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiskLimits(StrictModel):
    max_position_pct: float = Field(gt=0, le=100)
    max_order_pct: float = Field(gt=0, le=100)
    min_cash_pct: float = Field(ge=0, lt=100)
    max_daily_loss_pct: float = Field(gt=0, le=100)
    max_drawdown_pct: float = Field(gt=0, le=100)


class LeveragePolicy(StrictModel):
    enabled: bool = False
    max_gross_exposure_multiple: float = Field(default=1.0, ge=1.0)
    allowed_products: list[str] = Field(default_factory=list)


class AgentRiskPolicy(StrictModel):
    overnight_positions: bool = False
    short_selling: bool = False
    leverage: LeveragePolicy = Field(default_factory=LeveragePolicy)


class KillSwitchPolicy(StrictModel):
    enabled: bool = True
    fail_closed: bool = True


class SimulatorPolicy(StrictModel):
    fee_bps: float = Field(default=10, ge=0, le=1000)
    slippage_bps: float = Field(default=5, ge=0, le=1000)
    spread_bps: float = Field(default=10, ge=0, le=2000)


class RiskConfig(StrictModel):
    defaults: RiskLimits
    agents: dict[str, AgentRiskPolicy] = Field(default_factory=dict)
    kill_switch: KillSwitchPolicy
    market_data_max_age_seconds: int = Field(default=120, gt=0, le=3600)
    starting_capital_eur: float = Field(default=10000, gt=0)
    simulator: SimulatorPolicy = Field(default_factory=SimulatorPolicy)

    def for_agent(self, agent_id: str) -> AgentRiskPolicy:
        return self.agents.get(agent_id, AgentRiskPolicy())


class ExperimentConfig(StrictModel):
    name: str = Field(min_length=1)
    base_currency: str = Field(min_length=3, max_length=3)
    starting_capital_eur: float = Field(gt=0)
    execution_mode: str
    portfolios: list[str] = Field(min_length=1)


def load_risk_config(
    path: Path | None = None, experiment_path: Path | None = None
) -> RiskConfig:
    config_path = path or Path(__file__).resolve().parents[2] / "config" / "risk" / "risk.yaml"
    with config_path.open("r", encoding="utf-8") as file:
        document = yaml.safe_load(file)
    if not isinstance(document, dict) or set(document) != {"risk"}:
        raise ValueError("Risk-Konfiguration muss genau einen 'risk'-Abschnitt enthalten")
    risk = document["risk"]

    experiment_config_path = experiment_path or (
        Path(__file__).resolve().parents[2] / "config" / "experiment.yaml"
    )
    with experiment_config_path.open("r", encoding="utf-8") as file:
        experiment_document = yaml.safe_load(file)
    if not isinstance(experiment_document, dict) or set(experiment_document) != {
        "experiment"
    }:
        raise ValueError(
            "Experiment-Konfiguration muss genau einen 'experiment'-Abschnitt enthalten"
        )
    experiment = ExperimentConfig.model_validate(experiment_document["experiment"])
    return RiskConfig.model_validate(
        {**risk, "starting_capital_eur": experiment.starting_capital_eur}
    )
