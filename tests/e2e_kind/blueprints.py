"""Applying the example blueprints to the cluster, exactly as a person
running the platform does, and waiting for the company to converge — a
phase the operator writes only once OPA serves the applied policy, not
when the pods are Ready."""

import tempfile
from collections.abc import Callable
from pathlib import Path

import yaml

from .cluster import (
    COMPANY,
    COMPANY_NS,
    DELEGATED,
    EXAMPLES,
    HELLO_SAAS,
    REPO_ROOT,
    agent_image,
    await_bare_heartbeat_asleep,
    console,
    harness_repo_or_fail,
    kubectl,
)

__all__ = ["DELEGATED", "EXAMPLES", "HELLO_SAAS", "REPO_ROOT"]

# The suite's derived blueprints, written here and removed with the process.
_SCRATCH = tempfile.TemporaryDirectory(prefix="poseidon-bp-")

# The examples declare the waits a real company gives its humans (a working
# day at the merge gate, an hour at a budget stop). The suite waits those
# gates out to watch them escalate and expire, so every document it applies
# carries these instead: escalation lands at half a minute, and the budget
# approve path has time to re-apply a blueprint before its pause ends.
E2E_GATE_MINUTES = 1
E2E_PAUSE_MINUTES = 2


def derived_blueprint(base: Path, mutate: Callable[[dict], None], label: str) -> Path:
    """A harness-local variant of an example blueprint: the example's
    document with one declared difference, written to a temp file for
    `apply_blueprint`. The module story stays "the example, minus the
    piece this module must not share its lane with"."""
    document = yaml.safe_load(base.read_text())
    mutate(document)
    path = Path(_SCRATCH.name) / f"{base.stem}-{label}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def may_start(base: Path, *workflows: str) -> Path:
    """`base` with the heartbeat's initiative narrowed to `workflows`. The
    one lane every module shares is the standing heartbeat's; what it may
    start on its own is the blueprint's to say, so a module that must not
    have its delivered PR reviewed on the next wake, or its ticket
    implemented before a person does, declares that — never a company
    without a heartbeat, which does not compile."""

    def narrow(document: dict) -> None:
        document["heartbeat"]["may_start"] = list(workflows)

    return derived_blueprint(base, narrow, "-".join(workflows))


def single_attempt(path: Path) -> Path:
    """`path` with `max_attempts: 1`: a run that ends without a conclusion
    is the company's last word on its item. The loop retries an aborted run
    on its own, so a module whose run is *meant* to fail declares the
    one-shot company rather than racing the retry."""

    def one(document: dict) -> None:
        document["heartbeat"]["max_attempts"] = 1

    return derived_blueprint(path, one, "one-shot")


def pristine_profile() -> Path:
    """The company the suite rests at and every module returns to:
    `hello-saas.yaml` with review off the heartbeat's own initiative. A delivered PR then waits for the
    world instead of being reviewed on the next wake — a review parked on
    the human gate would hold the one concurrency slot and race the
    fixture closing the PR. Modules whose arc reviews apply the example
    as shipped."""
    return may_start(HELLO_SAAS, "scope", "implement")


def harness_repo() -> str:
    """The disposable repository this suite works on — the one value the
    harness owns rather than the blueprint, so it lives in the suite's own
    environment variable and not in the deployment's env file."""
    return harness_repo_or_fail()


def suite_document(path: Path) -> Path:
    """The document at `path` as the suite applies it: the disposable
    repository in place of the example's, and the suite's gate waits in
    place of the human-scale ones — every gate's `deadline_minutes` and the
    budget `pause_minutes`. Nothing else changes: the suite applies the
    example's authority, on the suite's repository and clock."""
    document = yaml.safe_load(path.read_text())
    document["stack"]["repo"]["url"] = harness_repo()
    for node in (document.get("workflows") or {}).values():
        for gate in (node.get("gates") or {}).values():
            gate["deadline_minutes"] = E2E_GATE_MINUTES
    document["budgets"]["per_run"]["pause_minutes"] = E2E_PAUSE_MINUTES
    out = Path(_SCRATCH.name) / f"{path.stem}-e2e.yaml"
    out.write_text(yaml.safe_dump(document, sort_keys=False))
    return out


def apply_blueprint(path: Path) -> None:
    """Apply the suite's version of `path` (see `suite_document`) through
    the console, as the human `owner` — the act a person performs: the
    console compiles it against the live registry, decides, submits the
    CRs; the operator converges them."""
    result = console().apply(suite_document(path).read_text())
    assert result["company"] == COMPANY, result


def wait_for_convergence(timeout_s: int = 300) -> None:
    """The company namespace is Active, the Company reads `converged` for
    the generation just applied — the operator writes that phase only once
    OPA serves the stored bundle revisions, the platform's and the
    company's — and its one standing agent (the PM) is rolled out. The
    generation matters after a re-apply: the CR still carries the previous
    apply's `converged` until the operator has looked at the new spec."""
    kubectl(
        "wait",
        "--for=jsonpath={.status.phase}=Active",
        f"ns/{COMPANY_NS}",
        f"--timeout={timeout_s}s",
        timeout=timeout_s + 30,
    )
    generation = kubectl(
        "get", "company", COMPANY, "-o", "jsonpath={.metadata.generation}"
    )
    for condition in (
        f"jsonpath={{.status.observedGeneration}}={generation}",
        "jsonpath={.status.phase}=converged",
    ):
        kubectl(
            "wait",
            f"--for={condition}",
            f"company/{COMPANY}",
            f"--timeout={timeout_s}s",
            timeout=timeout_s + 30,
        )
    kubectl(
        "-n",
        COMPANY_NS,
        "rollout",
        "status",
        "deployment/pm",
        f"--timeout={timeout_s}s",
        timeout=timeout_s + 30,
    )
    assert_company_pods_run_this_build()


def assert_company_pods_run_this_build() -> None:
    """Every live company pod built from one of our images carries the
    build the worker was deployed with. kind-up.sh asserts this for the
    platform's pods; the company's exist only once a blueprint is applied,
    so the suite asserts it here, at convergence. Without it a stale
    `:latest` standing agent keeps executing code you already fixed, and
    the only symptom is a test failing for no visible reason. Finished
    sandbox Jobs and pods on their way out are not running stale code."""
    tag = agent_image().rsplit(":", 1)[-1]
    rows = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "pods",
        "-o",
        "jsonpath={range .items[*]}{.status.phase}|{.metadata.deletionTimestamp}"
        '|{.metadata.name}|{range .spec.containers[*]}{.image} {end}{"\\n"}{end}',
    )
    stale = []
    for row in rows.splitlines():
        phase, deleting, name, images = row.split("|", 3)
        if phase in ("Succeeded", "Failed") or deleting:
            continue
        ours = [i for i in images.split() if "poseidon-" in i]
        if any(not i.endswith(f":{tag}") for i in ours):
            stale.append(f"{name}: {' '.join(ours)}")
    assert not stale, f"company pods not running build {tag}:\n" + "\n".join(stale)


def bundle_revision_from_cr() -> str:
    return kubectl("get", "company", COMPANY, "-o", "jsonpath={.status.bundleRevision}")


def restore_pristine() -> None:
    """Back to the pristine profile, converged and asleep — what every
    module that applied another profile owes the modules after it. Always
    re-applies and waits for convergence (OPA bundle activation included):
    a restore that skipped the wait could hand the next module a company
    that never re-converged. The standing heartbeat reads the re-applied
    section on its next tick and keeps running: a company always has one."""
    apply_blueprint(pristine_profile())
    wait_for_convergence()
    await_bare_heartbeat_asleep()
