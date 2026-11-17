"""Reward operations: python -m services.reward_engine.cli."""

import argparse
from datetime import UTC, datetime

from trading_core.db import make_engine, make_session_factory
from trading_core.reward_config import load_reward_config

from .service import evaluate_reward


def _parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise argparse.ArgumentTypeError("Zeitpunkt muss eine Zeitzone enthalten")
    return result.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reward-Bewertung eines Zeitraums")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--period-start", required=True, type=_parse_time)
    parser.add_argument("--period-end", required=True, type=_parse_time)
    parser.add_argument("--benchmark-agent-id", default="benchmark-d")
    args = parser.parse_args()

    engine = make_engine(pool_pre_ping=True)
    session_factory = make_session_factory(engine)
    try:
        with session_factory() as session:
            reward = evaluate_reward(
                session,
                args.agent_id,
                args.period_start,
                args.period_end,
                load_reward_config(),
                benchmark_agent_id=args.benchmark_agent_id,
            )
            print(
                f"agent={reward.agent_id} period={reward.period_start.isoformat()}.."
                f"{reward.period_end.isoformat()} version={reward.reward_version} "
                f"net_return={reward.net_return} benchmark={reward.benchmark_return} "
                f"reward={reward.reward_value}"
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
