"""The merge gate's guarantees, proven at the workflow layer: the real
ReviewWorkflow runs against Temporal's time-skipping test environment with
scripted activities — sequencing the activity units cannot see.

What must hold:

- both layers required: a steered pass verdict with red platform checks
  merges nothing, green checks with a fail verdict merge nothing — each
  feeds the bounded rework loop instead;
- a carve-out path parks on the human signal even with perfect layers
  (the OPA half of the same guarantee is pinned in the compiler's
  bundle-decision tests);
- revocation mid-flight: once the live bundle no longer carries the
  automation, resolve_merge denies and the run flips to human-only
  cleanly — config read at run start grants nothing by itself;
- the human `merge` signal still resolves every gate: overrides a
  decline, decides an exhausted rework chain, and a gate without
  `automate` never touches the automation machinery at all;
- a contract-tree PR is the human's to merge by construction: the gate
  declines before spending any layer — no verify run, no composed
  baseline, no resolve evaluation, no rework ticket — and the human's
  approving verdict still releases it (the OPA carve-out deny is pinned in the
  compiler's bundle tests; the layer tests above pin the contract fact's
  place in the layers for every other diff).

Authority-not-looser is pinned where it lives: `git.merge_pr` ungrantable
(schema + bundle tests), the conductor's `may_start` enum (schema tests),
the platform-only rework write (bundle tests).
"""

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import pytest
from conftest import state_is, until
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from orchestrator.workflows.base import FAST_TIMEOUTS
from orchestrator.workflows.review import ReviewInput, ReviewWorkflow

pytestmark = pytest.mark.anyio

RUN_ID = "run-review-wf"
# The bounds the run's shape carries onto every dispatch.
BOUNDS = "Repository: github.com/example/repo, base ref main."
PR = 41
# The states a review reports while parked on each gate.
MERGE_PARK = "parked-merge_gate"
BUDGET_PARK = "parked-budget_exhausted"
# The head commit read_pr reports: every fetch and the merge name it.
HEAD_SHA = "f00d5ha"
# The base branch's tip the review ran against.
BASE_SHA = "ba5e000"

AUTOMATED_GATE = {
    "deadline_minutes": 1,
    "automate": {"paths": ["src/", "tests/"], "max_reworks": 2},
}
HUMAN_GATE = {"deadline_minutes": 1}
# A person's recorded rejection, as the console wrote it.
REJECTED = {
    "principal": "owner",
    "approved": False,
    "rationale": "guts the spec",
    "ts": "t1",
}


def dispatch_result(**overrides: Any) -> dict[str, Any]:
    result = {
        "status": "done",
        "error": None,
        "comment_urls": ["https://pr/41#c-1"],
        "verdict": "pass",
        "verdict_rationale": "Suite green; in scope.",
    }
    result.update(overrides)
    return result


@dataclass
class Harness:
    """Scripted activities + captured effects for one review run."""

    merge_gate: dict[str, Any] | None = None
    dispatch: dict[str, Any] = field(default_factory=dispatch_result)
    verify: dict[str, Any] = field(default_factory=lambda: {"passed": True})
    pr_paths: list[str] = field(
        default_factory=lambda: ["src/slugify.py", "tests/test_slugify.py"]
    )
    contract_paths: list[str] = field(default_factory=list)
    # What the base ref holds of the declared tree; None means all of it.
    contract_tree: list[str] | None = None
    contract_verify: dict[str, Any] = field(default_factory=lambda: {"passed": True})
    deletions: dict[str, int] | None = None
    # "merged" / "closed" makes read_pr refuse the PR as the real activity
    # does when the git host says it is no longer open.
    pr_settled: str | None = None
    independence: dict[str, Any] = field(
        default_factory=lambda: {"independent": True, "reason": "lineage run-impl"}
    )
    resolve: dict[str, Any] = field(
        default_factory=lambda: {"allow": True, "rule": "merge_resolve_automated"}
    )
    # The console's recorded verdict the human path reads back (None: no
    # human ever answered through the console), and what the compiled
    # human path decides on it.
    human_verdict: dict[str, Any] | None = field(
        default_factory=lambda: {"principal": "owner", "approved": True, "ts": "t0"}
    )
    human_resolve: dict[str, Any] = field(
        default_factory=lambda: {"allow": True, "rule": "merge_resolve_human"}
    )
    depth: dict[str, Any] = field(default_factory=lambda: {"depth": 0, "root_pr": PR})
    # Someone pushed to the PR branch after the run read its head: the
    # merge site's read finds the head moved — or, pushed after that read,
    # the host refuses the pinned merge.
    head_moved: bool = False
    head_moved_at_merge: bool = False
    # The base tips the merge site reads, one per read; the review's own
    # base once they run out.
    bases: list[str] = field(default_factory=list)
    # The head merged onto a moved base: whether it conflicts, and how its
    # suite runs (green unless stated).
    merge_conflicts: bool = False
    merge_verify: dict[str, Any] = field(default_factory=lambda: {"passed": True})
    # The bases each merge verify built the head onto.
    merge_verifies: list[str] = field(default_factory=list)
    # The tree the sandbox was last provisioned from.
    provisioned: str = ""

    verify_calls: int = 0
    scoped_verifies: list[list[str]] = field(default_factory=list)
    fetches: list[list[Any]] = field(default_factory=list)
    provisions: int = 0
    warns: list[dict[str, int]] = field(default_factory=list)
    independence_calls: int = 0
    resolve_args: list[list[Any]] = field(default_factory=list)
    depth_calls: int = 0
    reworks: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[tuple[str, str]] = field(default_factory=list)
    gate_events: list[str] = field(default_factory=list)
    merges: list[int] = field(default_factory=list)
    # The head commit each merge named.
    merge_heads: list[str] = field(default_factory=list)
    # The world-changing platform acts in order: the merge, the closing of
    # the tickets it fulfilled, a rework ticket and the PR it supersedes,
    # a refused delivery.
    effects: list[str] = field(default_factory=list)
    # The reasons each refused delivery was refused with, in order.
    refusals: list[str] = field(default_factory=list)
    # A ticket close the gateway keeps refusing.
    close_fails: bool = False
    torn_down: list[tuple[dict[str, Any] | None, str | None]] = field(
        default_factory=list
    )
    # The sandbox handle each teardown was given.
    torn_down_handles: list[dict[str, Any] | None] = field(default_factory=list)
    # Each give-back before a merge park: (handle, keep_slot, the gate
    # outcomes recorded before it).
    vacated: list[tuple[dict[str, Any] | None, bool, list[str]]] = field(
        default_factory=list
    )
    # The runs that asked the platform, after teardown, whether they spent
    # the last attempt on their PR — only runs that reached no verdict.
    attempts_recorded: list[str] = field(default_factory=list)
    wakes: int = 0
    # A dispatch that never answers, so a test can cancel the run mid-flight.
    dispatch_hangs: bool = False
    # The first dispatch hits the run's budget cap and parks on `budget`.
    budget_capped: bool = False
    dispatches: int = 0
    # The head verify and the reviewer dispatch, in the order they ran.
    sequence: list[str] = field(default_factory=list)
    # (procedure, documents, bounds) per dispatch.
    carried: list[tuple[str, dict[str, str], str]] = field(default_factory=list)

    def activities(self) -> list[Any]:
        harness = self

        @activity.defn(name="create_run")
        async def create_run(run_id: str, workflow: str, task: str) -> None: ...

        @activity.defn(name="check_budget")
        async def check_budget(run_id: str) -> dict[str, Any]:
            return {"cap_usd": 5.0, "spent_usd": 0.0}

        @activity.defn(name="workflow_config")
        async def workflow_config(
            workflow: str, crewless: bool = False
        ) -> dict[str, Any]:
            return {
                "company": "hello-saas",
                "conductor": {"agent_id": "pm-1", "role": "pm", "url": "http://pm:8000"},
                "crew": "feature-review",
                "gates": {"merge": harness.merge_gate} if harness.merge_gate else {},
                "max_verify_retries": 0,
                "budget_deadline_minutes": 1,
                "contract_paths": list(harness.contract_paths),
                "bounds": BOUNDS,
                "timeouts": asdict(FAST_TIMEOUTS),
            }

        @activity.defn(name="read_pr")
        async def read_pr(
            run_id: str, pr_number: int, contract_paths: list[str]
        ) -> dict[str, Any]:
            if harness.pr_settled:
                raise ApplicationError(
                    f"pr_not_open: pull request #{pr_number} is"
                    f" {harness.pr_settled} — nothing to review",
                    non_retryable=True,
                )
            return {
                "number": pr_number,
                "title": "Add slugify utility",
                "head": "feature/slugify-x",
                "head_sha": HEAD_SHA,
                "base_sha": BASE_SHA,
                "url": f"https://git.test/r/pulls/{pr_number}",
                "paths": list(harness.pr_paths),
                "contract_deletions": harness.deletions,
            }

        @activity.defn(name="pr_tips")
        async def pr_tips(run_id: str, pr_number: int) -> dict[str, Any]:
            base = harness.bases.pop(0) if harness.bases else BASE_SHA
            head = "pu5h3d" if harness.head_moved else HEAD_SHA
            return {"head_sha": head, "base_sha": base, "settled": None}

        @activity.defn(name="warn_contract_edit")
        async def warn_contract_edit(
            run_id: str, pr_number: int, deletions: dict[str, int], workflow_id: str
        ) -> None:
            harness.warns.append(deletions)

        @activity.defn(name="contract_tree")
        async def contract_tree(run_id: str, contract_paths: list[str]) -> list[str]:
            if harness.contract_tree is None:
                return list(contract_paths)
            return list(harness.contract_tree)

        @activity.defn(name="fetch_workspace")
        async def fetch_workspace(
            run_id: str,
            ref: str,
            overlay_paths: list[str] | None = None,
            merge_onto: str | None = None,
        ) -> str:
            if merge_onto:
                harness.merge_verifies.append(merge_onto)
                if harness.merge_conflicts:
                    raise ApplicationError(
                        f"merge conflict: {ref} does not merge cleanly onto"
                        f" {merge_onto} (conflicts in slugify.py)",
                        type="merge conflict",
                        non_retryable=True,
                    )
                return "merged-tar"
            harness.fetches.append([ref, overlay_paths])
            return "workspace-tar"

        @activity.defn(name="provision_sandbox")
        async def provision_sandbox(run_id: str, tar: str) -> dict[str, Any]:
            harness.provisions += 1
            harness.provisioned = tar
            return {"sandbox": "sbx-1"}

        @activity.defn(name="fulfill_crew")
        async def fulfill_crew(request: dict[str, Any]) -> dict[str, Any]:
            return {
                "agents": {
                    "reviewer": {"agent_id": "fr-reviewer", "url": "http://crew:8000"}
                },
            }

        @activity.defn(name="dispatch_documents")
        async def dispatch_documents(run_id: str) -> dict[str, str]:
            return {"strategy": "Ship slugs."}

        @activity.defn(name="dispatch_agent")
        async def dispatch_agent(
            run_id: str,
            url: str,
            procedure: str,
            task_text: str,
            bounds: str,
            documents: dict[str, str],
        ) -> dict[str, Any]:
            harness.dispatches += 1
            harness.sequence.append("dispatch")
            harness.carried.append((procedure, documents, bounds))
            while harness.dispatch_hangs:
                # Heartbeat so the workflow's cancel reaches the activity.
                activity.heartbeat()
                await asyncio.sleep(0.05)
            if harness.budget_capped and harness.dispatches == 1:
                return {"status": "failed", "error": "budget_exhausted: cap reached"}
            return dict(harness.dispatch)

        @activity.defn(name="record_verdict")
        async def record_verdict(
            run_id: str, verdict: str, rationale: str, workflow_id: str
        ) -> None:
            harness.verdicts.append((verdict, rationale))

        @activity.defn(name="platform_verify")
        async def platform_verify(
            run_id: str, paths: list[str] | None = None
        ) -> dict[str, Any]:
            if paths:
                harness.scoped_verifies.append(list(paths))
                return dict(harness.contract_verify)
            if harness.provisioned == "merged-tar":
                harness.sequence.append("merge verify")
                return dict(harness.merge_verify)
            harness.verify_calls += 1
            harness.sequence.append("verify")
            return dict(harness.verify)

        @activity.defn(name="reviewer_independent")
        async def reviewer_independent(run_id: str, pr_number: int) -> dict[str, Any]:
            harness.independence_calls += 1
            return dict(harness.independence)

        @activity.defn(name="read_human_verdict")
        async def read_human_verdict(run_id: str) -> dict[str, Any] | None:
            return dict(harness.human_verdict) if harness.human_verdict else None

        @activity.defn(name="resolve_merge")
        async def resolve_merge(
            run_id: str, resolver: str, facts: dict[str, Any], workflow_id: str
        ) -> dict[str, Any]:
            harness.resolve_args.append([resolver, facts])
            if resolver == "human":
                return dict(harness.human_resolve)
            return dict(harness.resolve)

        @activity.defn(name="rework_depth")
        async def rework_depth(run_id: str, pr_number: int) -> dict[str, Any]:
            harness.depth_calls += 1
            return dict(harness.depth)

        @activity.defn(name="file_rework")
        async def file_rework(
            run_id: str,
            pr_number: int,
            root_pr: int,
            attempt: int,
            reason: str,
            findings: str,
            pr_title: str,
            pr_url: str,
            paths: list[str],
        ) -> dict[str, Any]:
            harness.effects.append("file_rework")
            harness.reworks.append(
                {
                    "root_pr": root_pr,
                    "attempt": attempt,
                    "reason": reason,
                    "findings": findings,
                }
            )
            return {"url": "https://git.test/r/issues/91", "number": 91}

        @activity.defn(name="record_gate_outcome")
        async def record_gate_outcome(
            run_id: str, gate: str, kind: str, workflow_id: str
        ) -> None:
            harness.gate_events.append(kind)

        @activity.defn(name="close_superseded_pr")
        async def close_superseded_pr(
            run_id: str, pr_number: int, ticket_number: int
        ) -> dict[str, Any]:
            harness.effects.append(f"close_pr #{pr_number} for #{ticket_number}")
            return {"number": pr_number, "state": "closed"}

        @activity.defn(name="merge_pr")
        async def merge_pr(run_id: str, pr_number: int, head_sha: str) -> dict[str, Any]:
            harness.merge_heads.append(head_sha)
            if harness.head_moved_at_merge:
                return {
                    "merged": False,
                    "sha": "",
                    "refused": f"head moved after review: pull request #{pr_number}"
                    f" is no longer at {head_sha}",
                }
            harness.merges.append(pr_number)
            harness.effects.append("merge")
            return {"merged": True, "sha": "abc123"}

        @activity.defn(name="refuse_delivery")
        async def refuse_delivery(run_id: str, pr_number: int, reason: str) -> None:
            harness.effects.append(f"refuse #{pr_number}")
            harness.refusals.append(reason)

        @activity.defn(name="close_tickets")
        async def close_tickets(
            run_id: str, pr_number: int, sha: str, pr_title: str
        ) -> list[int]:
            if harness.close_fails:
                raise ApplicationError("issues.close: backend down", non_retryable=True)
            harness.effects.append(f"close_tickets #{pr_number} {sha} {pr_title}")
            return [7]

        @activity.defn(name="abandon_exhausted")
        async def abandon_exhausted(run_id: str, workflow_id: str) -> dict:
            harness.attempts_recorded.append(run_id)
            return {"exhausted": False}

        @activity.defn(name="vacate")
        async def vacate(
            run_id: str, handle: dict[str, Any] | None, keep_slot: bool
        ) -> None:
            harness.vacated.append((handle, keep_slot, list(harness.gate_events)))

        @activity.defn(name="teardown")
        async def teardown(
            run_id: str,
            handle: dict[str, Any] | None,
            result: dict[str, Any] | None,
            error: str | None,
        ) -> dict[str, Any]:
            harness.torn_down.append((result, error))
            harness.torn_down_handles.append(handle)
            # Spread first: the dispatch result carries its own status
            # field, and the record's status/error must win, like the
            # real teardown's honest terminal write — which also reads a
            # dispatch's own error when the workflow passed none.
            error = error or (result or {}).get("error")
            return {
                **(result or {}),
                "run_id": run_id,
                "status": "failed" if error else "done",
                "error": error,
            }

        @activity.defn(name="wake_heartbeat")
        async def wake_heartbeat() -> None:
            harness.wakes += 1

        return [
            create_run,
            check_budget,
            workflow_config,
            read_pr,
            contract_tree,
            warn_contract_edit,
            fetch_workspace,
            provision_sandbox,
            fulfill_crew,
            dispatch_documents,
            dispatch_agent,
            record_verdict,
            platform_verify,
            reviewer_independent,
            read_human_verdict,
            resolve_merge,
            rework_depth,
            file_rework,
            close_superseded_pr,
            refuse_delivery,
            pr_tips,
            record_gate_outcome,
            merge_pr,
            close_tickets,
            abandon_exhausted,
            vacate,
            teardown,
            wake_heartbeat,
        ]


def review_worker(client: Client, harness: Harness) -> tuple[Worker, str]:
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ReviewWorkflow],
        activities=harness.activities(),
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    return worker, task_queue


async def run_review(client: Client, harness: Harness) -> dict[str, Any]:
    worker, task_queue = review_worker(client, harness)
    async with worker:
        handle = await client.start_workflow(
            "review",
            ReviewInput(run_id=RUN_ID, pr_number=PR),
            id=f"review-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        return await handle.result()


async def start_review(client: Client, harness: Harness) -> tuple[Worker, WorkflowHandle]:
    worker, task_queue = review_worker(client, harness)
    await worker.__aenter__()
    handle = await client.start_workflow(
        "review",
        ReviewInput(run_id=RUN_ID, pr_number=PR),
        id=f"review-{uuid.uuid4().hex[:8]}",
        task_queue=task_queue,
    )
    return worker, handle


# -- both layers required ---------------------------------------------------


async def test_red_checks_file_rework_without_a_reviewer(env):
    """The model-free layer is required, and it comes first: a red head
    suite feeds the bounded rework loop before any reviewer is dispatched,
    so no reviewer can talk the run past it and none is paid for. The red
    suite is the review's conclusion, so no attempt is counted as lost."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE, verify={"passed": False, "output": "red"}
    )
    record = await run_review(env.client, harness)

    assert harness.dispatches == 0
    assert harness.verdicts == []
    assert harness.attempts_recorded == []
    assert harness.merges == []
    assert harness.resolve_args == []  # no OPA evaluation on a red layer
    assert harness.reworks == [
        {
            "root_pr": PR,
            "attempt": 1,
            "reason": "platform verify red",
            "findings": "red",
        }
    ]
    assert harness.gate_events == ["rework_filed"]
    # The ticket supersedes the PR; nothing merged, so no ticket closes.
    assert harness.effects == ["file_rework", f"close_pr #{PR} for #91"]
    assert record["status"] == "failed"
    assert "platform verify red" in record["error"]
    assert record["pr_url"] == f"https://git.test/r/pulls/{PR}"


async def test_red_checks_at_the_rework_bound_still_get_a_reviewer(env):
    """The chain is spent, so the red suite escalates: the reviewer still
    runs, so the human park carries its findings, and no automated layer is
    spent — no resolve, no independence read, straight to the human."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE,
        verify={"passed": False, "output": "red"},
        depth={"depth": 2, "root_pr": 38},
    )
    worker, handle = await start_review(env.client, harness)
    try:
        await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
        assert harness.sequence == ["verify", "dispatch"]
        assert harness.verdicts == [("pass", "Suite green; in scope.")]
        assert harness.gate_events == ["rework_exhausted", "parked"]
        await handle.signal("merge", args=[False])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    assert harness.reworks == []
    assert harness.independence_calls == 0
    assert harness.resolve_args == []
    assert harness.merges == []
    assert record["status"] == "failed"
    assert "merge gate rejected" in record["error"]


async def test_green_checks_with_a_fail_verdict_merge_nothing(env):
    """The independent-judgement layer is required: green checks do not
    carry a failed verdict, which feeds the rework loop before any further
    layer is spent."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE,
        dispatch=dispatch_result(verdict="fail", verdict_rationale="Blocking."),
    )
    record = await run_review(env.client, harness)

    assert harness.merges == []
    assert harness.verify_calls == 1
    assert harness.scoped_verifies == []
    assert harness.resolve_args == []
    assert harness.reworks == [
        {
            "root_pr": PR,
            "attempt": 1,
            "reason": "review verdict failed",
            "findings": "Blocking.",
        }
    ]
    assert harness.gate_events == ["rework_filed"]
    assert record["status"] == "failed"
    # The fail verdict is the review's conclusion: the PR is consumed by
    # the verdict on its row, and the attempt accounting is never asked.
    assert harness.verdicts == [("fail", "Blocking.")]
    assert harness.attempts_recorded == []


async def test_both_layers_green_and_allowed_releases_the_merge(env):
    """The full automated path: verdict journaled, checks green, independence
    established, compiled allow — the merge happens with no human signal
    and the gate outcome names the resolver."""
    harness = Harness(merge_gate=AUTOMATED_GATE)
    record = await run_review(env.client, harness)

    assert harness.verdicts == [("pass", "Suite green; in scope.")]
    # The platform's verify runs first, in the sandbox as provisioned: the
    # reviewer holds a shell there, so nothing it leaves behind can reach
    # the verify of record.
    assert harness.sequence == ["verify", "dispatch"]
    assert harness.verify_calls == 1
    assert harness.independence_calls == 1
    # The reviewer was dispatched under the bounds, with the documents.
    assert harness.carried == [("review", {"strategy": "Ship slugs."}, BOUNDS)]
    # The compiled decision saw the real facts, changed paths included.
    assert harness.resolve_args == [
        [
            "automated",
            {
                "verdict": "pass",
                "checks_green": True,
                "reviewer_independent": True,
                "contract_green": True,
                "paths": ["src/slugify.py", "tests/test_slugify.py"],
            },
        ]
    ]
    assert harness.gate_events == ["automated"]
    assert harness.merges == [PR]
    # The merge names the head the run read, the one every sandbox held.
    assert harness.merge_heads == [HEAD_SHA]
    # No contract tree declared: one plain head tree, one sandbox, no
    # scoped verify — and a base that held still needs no merge verify.
    assert harness.fetches == [[HEAD_SHA, None]]
    assert harness.provisions == 1
    assert harness.scoped_verifies == []
    assert harness.merge_verifies == []
    # The fulfilled tickets close strictly after the merge, naming it.
    assert harness.effects == ["merge", f"close_tickets #{PR} abc123 Add slugify utility"]
    assert record["status"] == "done"
    assert record["merged"] is True
    assert record["merge_sha"] == "abc123"
    assert record["closed_tickets"] == [7]
    assert harness.wakes == 1  # the concluded review frees the slot: a nudge


async def test_a_reviewers_architecture_request_reaches_teardown(env):
    """A ticket the reviewer had the platform file is the review run's:
    the dispatch result carries it through every exit to teardown, which
    records it on the run's row."""
    url = "https://github.test/example/repo/issues/77"
    harness = Harness(
        merge_gate=AUTOMATED_GATE, dispatch={**dispatch_result(), "ticket_urls": [url]}
    )
    await run_review(env.client, harness)
    (result, _error) = harness.torn_down[0]
    assert result is not None and result["ticket_urls"] == [url]


# -- the contract layer ------------------------------------------------------


async def test_a_red_contract_verify_feeds_rework_not_the_resolve(env):
    """The baseline layer is required: head suite green, verdict pass —
    and the base-ref contract run red still merges nothing. The failure
    feeds the bounded rework loop, and the merge rule is never even asked."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE,
        contract_paths=["tests/contract/"],
        contract_verify={"passed": False, "output": "contract red"},
    )
    record = await run_review(env.client, harness)

    assert harness.merges == []
    assert harness.resolve_args == []
    assert harness.scoped_verifies == [["tests/contract/"]]
    assert harness.reworks == [
        {
            "root_pr": PR,
            "attempt": 1,
            "reason": "contract verify red",
            "findings": "contract red",
        }
    ]
    assert record["status"] == "failed"
    assert "contract verify red" in record["error"]


async def test_a_green_contract_verify_rides_the_resolve(env):
    """The composed baseline is built and run only when a contract tree is
    declared: the workspace is re-fetched with the contract overlay, the
    sandbox re-provisioned, the verify scoped to the contract paths, and
    the fact rides the compiled decision."""
    harness = Harness(merge_gate=AUTOMATED_GATE, contract_paths=["tests/contract/"])
    record = await run_review(env.client, harness)

    # First fetch: the plain head tree for the reviewer. Second: the
    # composed baseline (head + base-ref contract tree), then a second
    # provision replacing the sandbox in place.
    assert harness.fetches == [
        [HEAD_SHA, None],
        [HEAD_SHA, ["tests/contract/"]],
    ]
    assert harness.provisions == 2
    assert harness.scoped_verifies == [["tests/contract/"]]
    assert harness.resolve_args == [
        [
            "automated",
            {
                "verdict": "pass",
                "checks_green": True,
                "reviewer_independent": True,
                "contract_green": True,
                "paths": ["src/slugify.py", "tests/test_slugify.py"],
            },
        ]
    ]
    assert harness.merges == [PR]
    assert record["merged"] is True


async def test_a_declared_tree_the_base_ref_lacks_replays_nothing(env):
    """A company declares its contract tree before it has written one:
    its first deliveries are documents on a tree with no tests. With
    nothing on the base ref to replay, no composed run is built and the
    fact rides the resolve vacuously true — as it would undeclared —
    until a human merges the first contract pull request."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE, contract_paths=["tests/contract/"], contract_tree=[]
    )
    record = await run_review(env.client, harness)

    assert harness.fetches == [[HEAD_SHA, None]]
    assert harness.provisions == 1
    assert harness.scoped_verifies == []
    assert harness.resolve_args[0][1]["contract_green"] is True
    assert harness.merges == [PR]
    assert record["merged"] is True


# -- a contract-tree PR is the human's to merge, before any layer -----------


async def test_a_contract_tree_pr_parks_before_any_layer_is_spent(env):
    """The workflow-layer half of the carve-out guarantee: a PR touching
    the declared contract tree is never an automated merge, so the gate
    declines positionally — a perfect verdict is journaled, then no verify
    run, no composed baseline, no resolve evaluation, no rework ticket —
    and the run parks on the human, who reads the deletion facts posted
    before the park. At bootstrap this is also what keeps the first
    contract PR off a composed baseline that has no tree to run yet."""
    deletions = {
        "removed_tests": 0,
        "added_tests": 4,
        "removed_asserts": 0,
        "added_asserts": 4,
    }
    harness = Harness(
        merge_gate=AUTOMATED_GATE,
        contract_paths=["tests/contract/"],
        pr_paths=["tests/contract/test_slugify_contract.py"],
        deletions=deletions,
    )
    worker, handle = await start_review(env.client, harness)
    try:
        await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
        assert harness.verdicts == [("pass", "Suite green; in scope.")]
        assert harness.warns == [deletions]
        assert harness.verify_calls == 0
        assert harness.scoped_verifies == []
        assert harness.fetches == [[HEAD_SHA, None]]
        assert harness.provisions == 1
        assert harness.independence_calls == 0
        assert harness.resolve_args == []
        assert harness.depth_calls == 0
        assert harness.reworks == []
        assert harness.gate_events == ["automation_declined", "parked"]
        assert harness.merges == []
        # The park keeps its place, so a one-place company implements
        # nothing before its contract lands; its crew and sandbox are
        # still given back, and no nudge is due.
        assert harness.vacated == [({"sandbox": "sbx-1"}, True, ["automation_declined"])]
        assert harness.wakes == 0
        await handle.signal("merge", args=[True])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    assert harness.torn_down_handles == [None]
    # The human's approving verdict is the one authority that merges a
    # contract-tree edit — and it still works, findings and facts already
    # on the PR.
    assert harness.gate_events == ["automation_declined", "parked", "approved"]
    assert harness.merges == [PR]
    assert record["status"] == "done"
    assert record["merged"] is True


async def test_a_rejected_contract_pr_fails_honestly(env):
    """The other half of the human's authority over the spec: a rejection
    merges nothing, refuses the delivery with the person's words, and the
    run finalizes failed — a gutted contract never slips through by
    timeout or default."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE,
        contract_paths=["tests/contract/"],
        pr_paths=["tests/contract/test_slugify_contract.py"],
        deletions={
            "removed_tests": 4,
            "added_tests": 1,
            "removed_asserts": 4,
            "added_asserts": 1,
        },
        human_verdict=REJECTED,
    )
    worker, handle = await start_review(env.client, harness)
    try:
        await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
        await handle.signal("merge", args=[False])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    assert harness.merges == []
    assert harness.reworks == []
    assert harness.effects == [f"refuse #{PR}"]
    assert harness.refusals == ["owner rejected the merge: guts the spec"]
    assert harness.gate_events == ["automation_declined", "parked", "rejected"]
    assert record["status"] == "failed"
    assert "merge gate rejected" in record["error"]


# -- carve-outs and revocation park on the human ---------------------------


async def test_an_automation_decline_parks_on_the_human_who_can_release(env):
    """A carve-out (or any path not automated) deny with perfect layers is a
    decline, not a failure: the run parks on the human signal, and the
    human's approving verdict still releases the merge — the override survives
    automation."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE,
        resolve={"allow": False, "rule": "merge_resolve_carved_out_path"},
    )
    worker, handle = await start_review(env.client, harness)
    try:
        await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
        assert harness.gate_events == ["automation_declined", "parked"]
        assert harness.merges == []
        await handle.signal("merge", args=[True])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    # The release is verified like any human one: the recorded verdict is
    # read back and decided on before the merge.
    assert [args[0] for args in harness.resolve_args] == ["automated", "human"]
    assert harness.gate_events == ["automation_declined", "parked", "approved"]
    assert harness.merges == [PR]
    assert record["status"] == "done"
    assert record["merged"] is True


async def test_revocation_mid_flight_flips_the_gate_to_human_only(env):
    """Config read at run start grants nothing by itself: once the live
    bundle no longer carries the automation (the blueprint diff), the
    resolve denies and the run is human-only — unanswered, it escalates
    and expires like any human gate, nothing merges, and the delivery is
    refused with the deadline nobody met."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE,
        resolve={"allow": False, "rule": "default_deny"},
    )
    record = await run_review(env.client, harness)

    assert harness.merges == []
    assert harness.gate_events == [
        "automation_declined",
        "parked",
        "escalated",
        "expired",
    ]
    assert harness.effects == [f"refuse #{PR}"]
    assert harness.refusals == ["nobody answered within 1 minutes"]
    assert record["status"] == "failed"
    assert "merge gate expired" in record["error"]


# -- the rework bound and the human's last word -----------------------------


async def test_an_exhausted_rework_chain_escalates_to_the_human(env):
    """At max_reworks the chain stops feeding the backlog: no new ticket,
    a rework_exhausted process-gate event, and the human decides — here,
    a rejection, which refuses the delivery and fails the run honestly
    with nothing merged."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE,
        dispatch=dispatch_result(verdict="fail", verdict_rationale="Still blocking."),
        depth={"depth": 2, "root_pr": 38},
        human_verdict=REJECTED,
    )
    worker, handle = await start_review(env.client, harness)
    try:
        await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
        assert harness.reworks == []
        assert harness.gate_events == ["rework_exhausted", "parked"]
        await handle.signal("merge", args=[False])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    assert harness.merges == []
    # No ticket filed, so the PR is not superseded: it is refused.
    assert harness.effects == [f"refuse #{PR}"]
    assert harness.gate_events == ["rework_exhausted", "parked", "rejected"]
    assert record["status"] == "failed"
    assert "merge gate rejected" in record["error"]


async def test_a_human_gate_never_touches_the_automation_machinery(env):
    """A gate without `automate` is the plain human workflow: no
    verify re-run, no independence read, no automated resolve, no rework
    counting — give back what the run holds, park, approve, verify the
    recorded verdict, merge."""
    harness = Harness(merge_gate=HUMAN_GATE)
    worker, handle = await start_review(env.client, harness)
    try:
        await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
        # Before the park is recorded, the run gives back its crew, its
        # sandbox and its place under `max_concurrent_runs`, and nudges
        # the heartbeat.
        assert harness.vacated == [({"sandbox": "sbx-1"}, False, [])]
        assert harness.wakes == 1
        await handle.signal("merge", args=[True])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    assert harness.verify_calls == 0
    assert harness.independence_calls == 0
    assert harness.resolve_args == [
        ["human", {"human_verdict": {"principal": "owner", "approved": True, "ts": "t0"}}]
    ]
    assert harness.depth_calls == 0
    assert harness.gate_events == ["parked", "approved"]
    assert harness.merges == [PR]
    assert harness.merge_verifies == []  # the base held still
    assert harness.effects == ["merge", f"close_tickets #{PR} abc123 Add slugify utility"]
    assert record["merged"] is True
    # Teardown has no sandbox left to destroy; the end is a second nudge.
    assert harness.torn_down_handles == [None]
    assert harness.wakes == 2


async def test_a_budget_park_gives_nothing_back(env):
    """The budget park re-dispatches into the same agent and sandbox once
    approved, so it holds both; only a merge park gives them back."""
    harness = Harness(merge_gate=None, budget_capped=True)
    worker, handle = await start_review(env.client, harness)
    try:
        await until(
            lambda: state_is(handle, BUDGET_PARK), "the review parked on its budget"
        )
        await handle.signal("budget", args=[True])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    assert harness.dispatches == 2
    assert harness.vacated == []
    assert harness.torn_down_handles == [{"sandbox": "sbx-1"}]
    assert record["status"] == "done"


@pytest.mark.parametrize(
    ("verdict", "outcomes"),
    [(False, ["parked", "rejected"]), (None, ["parked", "escalated", "expired"])],
)
async def test_a_budget_park_not_approved_ends_the_review_at_its_cap(
    env, verdict, outcomes
):
    """Rejected, or unanswered until the blueprint's deadline, a budget
    park is never dispatched again: the review reaches no verdict and no
    merge gate, the run finalizes failed on the cap, and the attempt
    accounting runs."""
    harness = Harness(merge_gate=HUMAN_GATE, budget_capped=True)
    worker, handle = await start_review(env.client, harness)
    try:
        await until(
            lambda: state_is(handle, BUDGET_PARK), "the review parked on its budget"
        )
        if verdict is not None:
            await handle.signal("budget", args=[verdict])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    assert harness.gate_events == outcomes
    assert harness.dispatches == 1
    assert harness.verdicts == [] and harness.merges == []
    assert record["status"] == "failed"
    assert record["error"] == "budget_exhausted: cap reached"
    assert harness.attempts_recorded == [RUN_ID]


async def test_a_close_that_keeps_failing_ends_the_run_with_the_merge_recorded(env):
    """The merge landed; the tickets did not close. The run ends in error
    rather than leaving the open ticket silent, and the record still says
    what was merged."""
    harness = Harness(merge_gate=AUTOMATED_GATE, close_fails=True)
    record = await run_review(env.client, harness)

    assert harness.merges == [PR]
    assert record["status"] == "failed"
    assert "issues.close" in record["error"]
    assert record["merged"] is True
    assert record["merge_sha"] == "abc123"


@pytest.mark.parametrize(
    ("gate", "moved"),
    [
        (HUMAN_GATE, "head_moved"),
        (AUTOMATED_GATE, "head_moved"),
        (HUMAN_GATE, "head_moved_at_merge"),
        (AUTOMATED_GATE, "head_moved_at_merge"),
    ],
)
async def test_a_head_that_moved_after_review_merges_nothing(env, gate, moved):
    """A push to the PR branch after the run read its head: both resolvers
    reach the one merge site, which reads the PR again and finds it — or,
    pushed after that read, the host refuses the pinned merge. The run
    fails with the refusal — no merge, no ticket closed, and the delivery
    refused, so its ticket comes back."""
    harness = Harness(merge_gate=gate, **{moved: True})
    worker, handle = await start_review(env.client, harness)
    try:
        if gate is HUMAN_GATE:
            await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
            await handle.signal("merge", args=[True])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    assert harness.merge_heads == ([] if moved == "head_moved" else [HEAD_SHA])
    assert harness.merges == []
    assert harness.effects == [f"refuse #{PR}"]
    assert harness.refusals[0].startswith("head moved after review")
    assert record["status"] == "failed"
    assert "head moved after review" in record["error"]


# -- a moved base is verified before the merge --------------------------------


async def _approved_review(env, harness: Harness) -> dict[str, Any]:
    """The review to its end, a person approving when the gate parks."""
    if harness.merge_gate is not HUMAN_GATE:
        return await run_review(env.client, harness)
    worker, handle = await start_review(env.client, harness)
    try:
        await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
        await handle.signal("merge", args=[True])
        return await handle.result()
    finally:
        await worker.__aexit__(None, None, None)


@pytest.mark.parametrize("gate", [HUMAN_GATE, AUTOMATED_GATE])
async def test_a_moved_base_is_verified_then_merged(env, gate):
    """The base moved since the review: the head merged onto the new base
    is built in a fresh sandbox and run by the platform, the sandbox given
    back, and — green, the base still there — the head merges, still
    pinned."""
    harness = Harness(merge_gate=gate, bases=["ba5e001", "ba5e001"])
    record = await _approved_review(env, harness)
    assert harness.merge_verifies == ["ba5e001"]
    assert harness.sequence[-1] == "merge verify"
    # The verify's sandbox is given back, the run's place untouched.
    assert harness.vacated[-1][:2] == ({"sandbox": "sbx-1"}, True)
    assert harness.merge_heads == [HEAD_SHA]
    assert record["merged"] is True
    assert harness.torn_down_handles == [None]


@pytest.mark.parametrize("gate", [HUMAN_GATE, AUTOMATED_GATE])
@pytest.mark.parametrize(
    ("scripted", "reason"),
    [
        (
            {"merge_verify": {"passed": False, "output": "1 failed"}},
            "the head merged onto the moved base ba5e001 is red: 1 failed",
        ),
        (
            {"merge_conflicts": True},
            f"merge conflict: {HEAD_SHA} does not merge cleanly onto ba5e001",
        ),
    ],
)
async def test_a_red_or_conflicting_merge_onto_a_moved_base_is_refused(
    env, gate, scripted, reason
):
    harness = Harness(merge_gate=gate, bases=["ba5e001"], **scripted)
    record = await _approved_review(env, harness)
    assert harness.merges == [] and harness.merge_heads == []
    assert harness.refusals[0].startswith(reason)
    assert record["status"] == "failed"


@pytest.mark.parametrize("gate", [HUMAN_GATE, AUTOMATED_GATE])
async def test_a_base_that_keeps_moving_is_refused(env, gate):
    """Each green verify finds the base moved again: after
    `MERGE_VERIFY_ROUNDS` verifies the merge is refused."""
    harness = Harness(merge_gate=gate, bases=["b1", "b2", "b3"])
    record = await _approved_review(env, harness)
    assert harness.merge_verifies == ["b1", "b2"]
    assert harness.merges == []
    assert harness.refusals == [
        "the base kept moving: it moved again after 2 verifies of the head merged onto it"
    ]
    assert record["status"] == "failed"


# -- the release is the recorded verdict, not the signal ---------------------


async def test_a_signal_without_a_recorded_verdict_merges_nothing(env):
    """A `merge` signal that reached the workflow by any road but the
    console — network reach to the engine — finds no recorded verdict:
    the resolve denies by name, the gate records it, nothing merges, the
    delivery is refused, and the run ends failed."""
    harness = Harness(
        merge_gate=HUMAN_GATE,
        human_verdict=None,
        human_resolve={"allow": False, "rule": "merge_resolve_no_human_verdict"},
    )
    worker, handle = await start_review(env.client, harness)
    try:
        await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
        await handle.signal("merge", args=[True])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)

    assert harness.resolve_args == [["human", {}]]
    assert harness.merges == []
    assert harness.gate_events == ["parked", "approved", "verdict_unverified"]
    assert harness.refusals == [
        "no verified human verdict (merge_resolve_no_human_verdict)"
    ]
    assert record["status"] == "failed"
    assert "no verified human verdict (merge_resolve_no_human_verdict)" in record["error"]


async def test_a_missing_verdict_fails_before_any_gate(env):
    """The platform-side belt on the reviewer's contract (every review
    ends in a verdict): a dispatch that somehow reports no verdict reaches
    no gate and merges nothing."""
    harness = Harness(merge_gate=AUTOMATED_GATE, dispatch=dispatch_result(verdict=None))
    record = await run_review(env.client, harness)

    assert harness.verdicts == []
    assert harness.merges == []
    assert harness.gate_events == []
    assert record["status"] == "failed"
    assert record["error"] == "reviewer finished without a verdict"
    # No verdict is no conclusion: the run asks, after teardown, whether
    # it spent the PR's last attempt.
    assert harness.attempts_recorded == [RUN_ID]


# -- an aborted review is not the company's last word on the PR --------------


async def test_a_reviewer_that_died_mid_flight_leaves_the_pr_for_another_try(env):
    """The finding this exists for: a dispatch that came back with an
    error and no verdict (a timed-out model call, the step cap) reached no
    conclusion. Teardown finalizes the run failed, then the attempt
    accounting runs — the PR is the heartbeat's to offer again, bounded
    by the blueprint's cap."""
    harness = Harness(
        merge_gate=AUTOMATED_GATE,
        dispatch=dispatch_result(
            status="failed", error="litellm.Timeout", verdict=None, comment_urls=[]
        ),
    )
    record = await run_review(env.client, harness)

    assert record["status"] == "failed"
    assert harness.verdicts == [] and harness.gate_events == []
    assert harness.attempts_recorded == [RUN_ID]
    assert harness.wakes == 1  # the reoffered PR is the heartbeat's to see


async def test_a_pass_the_human_rejected_is_a_conclusion(env):
    """A pass the human then rejected concluded the review: the run
    finalizes failed, and the attempt accounting is never asked — the PR
    is consumed by the verdict on its row, not by the attempt cap."""
    harness = Harness(merge_gate=HUMAN_GATE)
    worker, handle = await start_review(env.client, harness)
    try:
        await until(lambda: state_is(handle, MERGE_PARK), "the review parked")
        await handle.signal("merge", args=[False])
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)
    assert record["status"] == "failed"
    assert harness.verdicts == [("pass", "Suite green; in scope.")]
    assert harness.attempts_recorded == []


async def test_a_cancel_on_an_unconcluded_review_still_records_the_attempt(own_env):
    """A review cancelled while its reviewer is dispatched reached no
    verdict either: the cancellation surfaces as the dispatch's, the run
    finalizes failed on it, and the attempt accounting still runs after
    teardown — a cancel that spends the PR's last attempt is not a silent
    one. The hung dispatch outlives the run on its server, hence a server
    of its own."""
    harness = Harness(merge_gate=HUMAN_GATE, dispatch_hangs=True)
    worker, handle = await start_review(own_env.client, harness)
    try:
        await until(lambda: harness.dispatches, "the reviewer was dispatched")
        await handle.cancel()
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)
    assert record["status"] == "failed"
    assert "cancelled" in record["error"].lower()
    assert harness.verdicts == []
    assert harness.attempts_recorded == [RUN_ID]


async def test_a_settled_pr_is_refused_before_any_sandbox_is_spent(env):
    """A PR the world already merged or closed fails at the fetch step
    with the named rule — no workspace fetched, no sandbox provisioned,
    no crew, no gate — and the run record says why. The heartbeat never
    retries it because the git host says it is settled (the observation
    reads every candidate's live state), not because the run consumed
    it: a refusal reaches no verdict, so the attempt accounting runs like
    any other abort."""
    harness = Harness(merge_gate=HUMAN_GATE, pr_settled="merged")
    record = await run_review(env.client, harness)

    assert harness.fetches == []
    assert harness.provisions == 0
    assert harness.verdicts == []
    assert harness.gate_events == []
    assert harness.merges == []
    assert record["status"] == "failed"
    assert "pr_not_open: pull request #41 is merged" in record["error"]
    assert harness.attempts_recorded == [RUN_ID]
