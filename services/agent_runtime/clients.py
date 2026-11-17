"""HTTP-only clients; the agent runtime has no database connection."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ServiceCallError(RuntimeError):
    """A trusted local service could not complete a request."""


def _request_json(
    method: str,
    url: str,
    api_key: str,
    timeout_seconds: int,
    payload: dict | None = None,
) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            result = json.loads(response.read())
    except HTTPError as exc:
        raise ServiceCallError(
            f"Lokaler Dienst antwortete mit HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise ServiceCallError("Lokaler Dienst ist nicht erreichbar") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ServiceCallError("Lokaler Dienst lieferte ungültiges JSON") from exc
    if not isinstance(result, dict):
        raise ServiceCallError("Lokaler Dienst lieferte ein unerwartetes Antwortformat")
    return result


class DataAPIClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: int = 15):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def get_context(self, agent_id: str) -> dict:
        return _request_json(
            "GET",
            f"{self.base_url}/agents/{agent_id}/context",
            self.api_key,
            self.timeout_seconds,
        )


class GatewayClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: int = 15):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def _post(self, path: str, payload: dict) -> dict:
        return _request_json(
            "POST",
            f"{self.base_url}{path}",
            self.api_key,
            self.timeout_seconds,
            payload,
        )

    def start_run(self, agent_id: str, model: str, trigger: str) -> dict:
        return self._post(
            "/agent-runs",
            {"agent_id": agent_id, "model": model, "trigger": trigger},
        )

    def finish_run(self, run_id: str, payload: dict) -> dict:
        return self._post(f"/agent-runs/{run_id}/finish", payload)

    def record_decision(self, payload: dict) -> dict:
        return self._post("/decisions", payload)

    def submit_order(self, payload: dict) -> dict:
        return self._post("/orders", payload)
