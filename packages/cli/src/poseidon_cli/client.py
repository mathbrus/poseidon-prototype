"""`ConsoleClient`: the console's routes as methods.

One method per route of the console interface (`common.console`):
requests built from its request models, answers parsed into its response
models, so a field the console stops sending fails here, loudly, rather
than in a rendering three calls later. The console's refusals are
exceptions: `PolicyDenied` carries the rule policy named (a
`PolicyRefusal`), `ConsoleError` the status and the detail of every other
(`Problem`). Nothing here decides anything — the client is the
console's mouthpiece, and the console is policy's.
"""

import os
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from common.console import (
    ApplyResult,
    BlueprintRequest,
    BlueprintView,
    CancelRequest,
    CompanyDeleted,
    CompanyView,
    DeleteRequest,
    EventsPage,
    Health,
    HeartbeatView,
    PauseRequest,
    PauseState,
    PolicyRefusal,
    PreviewResult,
    Problem,
    ResumeRequest,
    RunCancelled,
    RunDetail,
    RunList,
    RunSpendView,
    RunStarted,
    SpendView,
    StartRunRequest,
    StoryView,
    VerdictRecorded,
    VerdictRequest,
    WhoAmI,
    Woken,
)
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
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"console answered {status}: {detail}")
        self.status = status
        self.detail = detail


class PolicyDenied(ConsoleError):
    """The console asked policy and policy said no; `rule` names why."""

    def __init__(self, detail: str, rule: str) -> None:
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

    def _call[M: BaseModel](
        self,
        method: str,
        path: str,
        answer: type[M],
        body: BaseModel | None = None,
        **kwargs: Any,
    ) -> M:
        if body is not None:
            kwargs["json"] = body.model_dump(mode="json")
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise _refusal(response)
        return answer.model_validate(response.json())

    # -- the chokepoint -------------------------------------------------------------

    def healthz(self) -> Health:
        """The open route: liveness and the version the console runs."""
        return self._call("GET", "/healthz", Health)

    def whoami(self) -> WhoAmI:
        return self._call("GET", "/whoami", WhoAmI)

    # -- the blueprint --------------------------------------------------------

    def preview(self, blueprint: str) -> PreviewResult:
        return self._call(
            "POST",
            "/blueprint/preview",
            PreviewResult,
            BlueprintRequest(blueprint=blueprint),
        )

    def apply(self, blueprint: str) -> ApplyResult:
        return self._call(
            "POST", "/blueprint/apply", ApplyResult, BlueprintRequest(blueprint=blueprint)
        )

    def blueprint(self) -> BlueprintView:
        return self._call("GET", "/blueprint", BlueprintView)

    def company(self) -> CompanyView:
        return self._call("GET", "/company", CompanyView)

    def delete_company(self, rationale: str, wait: bool = True) -> CompanyDeleted:
        return self._call(
            "DELETE",
            "/company",
            CompanyDeleted,
            DeleteRequest(rationale=rationale, wait=wait),
        )

    # -- runs -----------------------------------------------------------------

    def start_run(self, workflow: str, number: int) -> RunStarted:
        return self._call(
            "POST",
            "/runs",
            RunStarted,
            StartRunRequest(workflow=workflow, number=number),
        )

    def runs(self, since: str | None = None) -> RunList:
        return self._call("GET", "/runs", RunList, params=_since(since))

    def run(self, run_id: str) -> RunDetail:
        return self._call("GET", f"/runs/{run_id}", RunDetail)

    def story(self, run_id: str, decisions: bool = False) -> StoryView:
        params = {"decisions": "true"} if decisions else None
        return self._call("GET", f"/runs/{run_id}/story", StoryView, params=params)

    def spend(self, run_id: str) -> RunSpendView:
        return self._call("GET", f"/runs/{run_id}/spend", RunSpendView)

    def resolve(
        self, run_id: str, gate: str, approved: bool, rationale: str
    ) -> VerdictRecorded:
        return self._call(
            "POST",
            f"/runs/{run_id}/gates/{gate}",
            VerdictRecorded,
            VerdictRequest(approved=approved, rationale=rationale),
        )

    def cancel(self, run_id: str, rationale: str) -> RunCancelled:
        return self._call(
            "POST",
            f"/runs/{run_id}/cancel",
            RunCancelled,
            CancelRequest(rationale=rationale),
        )

    # -- the company ------------------------------------------------------------

    def heartbeat(self, since: str | None = None) -> HeartbeatView:
        return self._call("GET", "/heartbeat", HeartbeatView, params=_since(since))

    def wake(self) -> Woken:
        return self._call("POST", "/heartbeat/wake", Woken)

    def pause(self, rationale: str) -> PauseState:
        return self._call(
            "POST", "/heartbeat/pause", PauseState, PauseRequest(rationale=rationale)
        )

    def resume(self, rationale: str) -> PauseState:
        return self._call(
            "POST", "/heartbeat/resume", PauseState, ResumeRequest(rationale=rationale)
        )

    def spend_since(self, since: str | None = None) -> SpendView:
        return self._call("GET", "/spend", SpendView, params=_since(since))

    def events(
        self, after: int = 0, run_id: str | None = None, limit: int = 200
    ) -> EventsPage:
        params: dict[str, Any] = {"after": after, "limit": limit}
        if run_id:
            params["run"] = run_id
        return self._call("GET", "/events", EventsPage, params=params)


def _refusal(response: httpx.Response) -> "ConsoleError":
    """The console's refusal as the exception it means: a 403 is a
    `PolicyRefusal`, every other refusal a `Problem`. A body that is
    neither (a proxy's page, say) keeps its text as the detail."""
    try:
        body = response.json()
    except ValueError:
        return ConsoleError(response.status_code, response.text)
    if response.status_code == 403:
        try:
            refusal = PolicyRefusal.model_validate(body)
        except ValidationError:
            pass
        else:
            return PolicyDenied(refusal.detail, refusal.rule)
    try:
        return ConsoleError(response.status_code, Problem.model_validate(body).detail)
    except ValidationError:
        return ConsoleError(response.status_code, response.text)


def _since(since: str | None) -> dict[str, str]:
    return {"since": since} if since else {}
