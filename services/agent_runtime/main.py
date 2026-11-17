"""Entrypoint: python -m services.agent_runtime.main."""

import json

from trading_core.settings import AgentRuntimeSettings

from .clients import DataAPIClient, GatewayClient
from .openrouter import OpenRouterAdapter
from .service import run_once


def main() -> None:
    settings = AgentRuntimeSettings()
    data = DataAPIClient(
        settings.data_api_base_url,
        settings.data_api_key.get_secret_value(),
        settings.request_timeout_seconds,
    )
    gateway = GatewayClient(
        settings.gateway_base_url,
        settings.gateway_api_key.get_secret_value(),
        settings.request_timeout_seconds,
    )
    result = run_once(
        settings,
        data,
        gateway,
        OpenRouterAdapter(settings),
        trigger="manual",
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
