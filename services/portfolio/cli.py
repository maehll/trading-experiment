"""Portfolio operations: python -m services.portfolio.cli."""

import argparse
from datetime import UTC, datetime

from trading_core.db import make_engine, make_session_factory
from trading_core.risk import load_risk_config

from .service import capture_all_snapshots, initialize_benchmark


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise argparse.ArgumentTypeError("Zeitpunkt muss eine Zeitzone enthalten")
    return result.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lokale Portfolio-Bewertung")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot-all")
    snapshot_parser.add_argument("--as-of", type=_parse_time)
    benchmark_parser = subparsers.add_parser("init-benchmark")
    benchmark_parser.add_argument("--symbol", required=True)
    benchmark_parser.add_argument("--agent-id", default="benchmark-d")
    benchmark_parser.add_argument("--as-of", type=_parse_time)
    args = parser.parse_args()

    engine = make_engine(pool_pre_ping=True)
    session_factory = make_session_factory(engine)
    risk_policy = load_risk_config()
    try:
        if args.command == "snapshot-all":
            snapshots = capture_all_snapshots(
                session_factory, risk_policy, as_of=args.as_of
            )
            for snapshot in snapshots:
                print(
                    f"{snapshot.agent_id} {snapshot.as_of.isoformat()} "
                    f"value_eur={snapshot.total_value_eur}"
                )
        else:
            with session_factory() as session:
                benchmark = initialize_benchmark(
                    session,
                    args.symbol,
                    risk_policy,
                    agent_id=args.agent_id,
                    as_of=args.as_of,
                )
                print(
                    f"{benchmark.agent_id} symbol={benchmark.symbol} "
                    f"units={benchmark.units} started_at={benchmark.started_at.isoformat()}"
                )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
