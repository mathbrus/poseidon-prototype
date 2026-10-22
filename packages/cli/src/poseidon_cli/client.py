"""`ConsoleClient`: the console's routes as methods.

One method per route, JSON in and out, and the console's refusals as
exceptions: `PolicyDenied` carries the rule policy named, `ConsoleError`
carries the status and the detail for everything else. Nothing here
decides anything — the client is the console's mouthpiece, and the
console is policy's.
"""

import os
from pathlib import Path
from typing import Any

import httpx

from common.topology import CONSOLE_PORT, TEARDOWN_TIMEOUT_S

DEFAULT_URL = f"http://127.0.0.1:{CONSOLE_PORT}"
URL_VAR = "POSEIDON_CONSOLE_URL"
TOKEN_VAR = "POSEIDON_TOKEN"


def token_file() -> Path:
    """Where `poseidon login --save` keeps a token and the client reads
    one when the environment carries none: the person's own config
    directory, since the CLI is the console's client and not a repo
    tool. The environment variable, when set, always wins."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "poseidon" / "token"


def saved_token() -> str:
    try:
        return token_file().read_text().strip()
    except OSError:
        return ""


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
        # Long enough to outlast the console's own longest wait, the
        # teardown a delete sits through.
        timeout: float = TEARDOWN_TIMEOUT_S + 60,
    ) -> None:
        self.base_url = (base_url or os.environ.get(URL_VAR) or DEFAULT_URL).rstrip("/")
        # Where the token came from, for the message a refusal deserves:
        # a saved token that the console refuses is one to save afresh.
        self.token_source = "argument"
        if token is None:
            token = os.environ.get(TOKEN_VAR, "")
            self.token_source = "environment"
            if not token:
                token = saved_token()
                self.token_source = "file"
        self.token = token
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        # An apply waits for the reconciler to create the company namespace,
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

    # -- the chokepoint -------------------------------------------------------------

    def healthz(self) -> dict[str, Any]:
        """The open route: liveness and the version the console runs."""
        return self._call("GET", "/healthz")

    def whoami(self) -> dict[str, Any]:
        return self._call("GET", "/whoami")

    # -- the blueprint --------------------------------------------------------

    def preview(self, blueprint: str) -> dict[str, Any]:
        return self._call("POST", "/blueprint/preview", json={"blueprint": blueprint})

    def apply(self, blueprint: str) -> dict[str, Any]:
        return self._call("POST", "/blueprint/apply", json={"blueprint": blueprint})

    def company(self) -> dict[str, Any]:
        return self._call("GET", "/company")

    def delete_company(self, rationale: str, wait: bool = True) -> dict[str, Any]:
        return self._call(
            "DELETE", "/company", json={"rationale": rationale, "wait": wait}
        )

    # -- runs -----------------------------------------------------------------

    def start_run(self, workflow: str, number: int) -> dict[str, Any]:
        return self._call("POST", "/runs", json={"workflow": workflow, "number": number})

    def runs(self, since: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", "/runs", params=_since(since))

    def run(self, run_id: str) -> dict[str, Any]:
        return self._call("GET", f"/runs/{run_id}")

    def story(self, run_id: str, decisions: bool = False) -> list[dict[str, Any]]:
        params = {"decisions": "true"} if decisions else None
        return self._call("GET", f"/runs/{run_id}/story", params=params)

    def spend(self, run_id: str) -> dict[str, Any]:
        return self._call("GET", f"/runs/{run_id}/spend")

    def resolve(
        self, run_id: str, gate: str, approved: bool, rationale: str
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/runs/{run_id}/gates/{gate}",
            json={"approved": approved, "rationale": rationale},
        )

    def cancel(self, run_id: str, rationale: str) -> dict[str, Any]:
        return self._call("POST", f"/runs/{run_id}/cancel", json={"rationale": rationale})

    # -- the company ------------------------------------------------------------

    def heartbeat(self, since: str | None = None) -> dict[str, Any]:
        return self._call("GET", "/heartbeat", params=_since(since))

    def wake(self) -> dict[str, Any]:
        return self._call("POST", "/heartbeat/wake")

    def pause(self, rationale: str) -> dict[str, Any]:
        return self._call("POST", "/heartbeat/pause", json={"rationale": rationale})

    def resume(self, rationale: str) -> dict[str, Any]:
        return self._call("POST", "/heartbeat/resume", json={"rationale": rationale})

    def spend_since(self, since: str | None = None) -> dict[str, Any]:
        return self._call("GET", "/spend", params=_since(since))

    def events(
        self, after: int = 0, run_id: str | None = None, limit: int = 200
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"after": after, "limit": limit}
        if run_id:
            params["run"] = run_id
        return self._call("GET", "/events", params=params)


def _since(since: str | None) -> dict[str, str]:
    return {"since": since} if since else {}
