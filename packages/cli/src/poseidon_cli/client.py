"""`ConsoleClient`: the console's routes as methods.

One method per route, JSON in and out, and the console's refusals as
exceptions: `PolicyDenied` carries the rule policy named, `ConsoleError`
carries the status and the detail for everything else. Nothing here
decides anything — the client is the console's mouthpiece, and the
console is policy's.
"""

import os
from typing import Any

import httpx

from common.topology import CONSOLE_PORT

DEFAULT_URL = f"http://127.0.0.1:{CONSOLE_PORT}"
URL_VAR = "POSEIDON_CONSOLE_URL"
TOKEN_VAR = "POSEIDON_TOKEN"


class ConsoleError(Exception):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(f"console answered {status}: {detail}")
        self.status = status
        self.detail = detail


class PolicyDenied(ConsoleError):
    """The console asked policy and policy said no; `rule` names why."""

    def __init__(self, detail: Any, rule: str) -> None:
        super().__init__(403, detail)
        self.rule = rule


class ConsoleClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 600.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get(URL_VAR) or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get(TOKEN_VAR, "")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        # An apply waits for the operator to create the company namespace,
        # which can take a while on a fresh cluster; connecting must not.
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    @classmethod
    def from_env(cls) -> "ConsoleClient":
        return cls()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ConsoleClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport ------------------------------------------------------------

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {"detail": response.text}
            if response.status_code == 403 and "rule" in body:
                raise PolicyDenied(body.get("detail"), body["rule"])
            raise ConsoleError(response.status_code, body.get("detail", body))
        return response.json()

    # -- the door -------------------------------------------------------------

    def healthy(self) -> bool:
        try:
            return self._http.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    def whoami(self) -> dict[str, Any]:
        return self._call("GET", "/whoami")

    # -- the blueprint --------------------------------------------------------

    def preview(self, blueprint: str) -> dict[str, Any]:
        return self._call("POST", "/blueprint/preview", json={"blueprint": blueprint})

    def apply(self, blueprint: str) -> dict[str, Any]:
        return self._call("POST", "/blueprint/apply", json={"blueprint": blueprint})

    # -- runs -----------------------------------------------------------------

    def start_run(self, workflow: str, number: int) -> dict[str, Any]:
        return self._call("POST", "/runs", json={"workflow": workflow, "number": number})

    def runs(self, since: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", "/runs", params=_since(since))

    def run(self, run_id: str) -> dict[str, Any]:
        return self._call("GET", f"/runs/{run_id}")

    def story(self, run_id: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/runs/{run_id}/story")

    def spend(self, run_id: str) -> dict[str, Any]:
        return self._call("GET", f"/runs/{run_id}/spend")

    def resolve(
        self, run_id: str, gate: str, approved: bool, payload: str | None = None
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/runs/{run_id}/gates/{gate}",
            json={"approved": approved, "payload": payload},
        )

    def cancel(self, run_id: str, reason: str = "") -> dict[str, Any]:
        return self._call("POST", f"/runs/{run_id}/cancel", json={"reason": reason})

    # -- the company ------------------------------------------------------------

    def heartbeat(self, since: str | None = None) -> dict[str, Any]:
        return self._call("GET", "/heartbeat", params=_since(since))

    def wake(self) -> dict[str, Any]:
        return self._call("POST", "/heartbeat/wake")

    def spend_since(self, since: str | None = None) -> dict[str, Any]:
        return self._call("GET", "/spend", params=_since(since))

    def now(self) -> str:
        return self._call("GET", "/now")["now"]


def _since(since: str | None) -> dict[str, str]:
    return {"since": since} if since else {}
