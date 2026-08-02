"""The gateway tool surface a blueprint may name (overview §4.7).

The compiler checks every declared scope against this list (step 9: "every
tool scope names a registered gateway backend"), so a typo fails at compile
instead of at the first tool call. Tools not yet implemented are listed
here anyway — the blueprint describes the company the platform is growing
into, and the steps that add the backends (15) do not get to redefine the
vocabulary.
"""

# Existing at M1.
SANDBOX_TOOLS = [
    "sandbox.read_file",
    "sandbox.write_file",
    "sandbox.list_dir",
    "sandbox.exec",
    "sandbox.run_tests",
    "sandbox.git_bundle",
]
REPO_TOOLS = ["repo.archive"]
GIT_TOOLS = ["git.open_pr"]

# Land at plan step 15, with compiled policy coverage.
ISSUE_TOOLS = ["issues.create", "issues.read", "issues.update", "issues.comment"]
REVIEW_TOOLS = ["git.read", "git.comment"]
# Platform lifecycle only (O-1 A): the orchestrator calls it after the gate.
PLATFORM_TOOLS = ["git.merge_pr"]
# Standing conductors with may_spawn.
CREW_TOOLS = ["crew.request"]

KNOWN_TOOLS = frozenset(
    SANDBOX_TOOLS
    + REPO_TOOLS
    + GIT_TOOLS
    + ISSUE_TOOLS
    + REVIEW_TOOLS
    + PLATFORM_TOOLS
    + CREW_TOOLS
)

# Tools no company role may declare: the platform identity holds them, and a
# blueprint that could grant them would make the merge gate optional.
PLATFORM_ONLY = frozenset(PLATFORM_TOOLS)

# Tools whose compiled rules additionally require a verified run binding in
# the decision input (§5.1) — a role allow-list without run binding reopens
# cross-run tool use, the protection `data.runs.assignments` used to give.
# A property of the tool, not a name prefix: whether `git.open_pr` joins
# this set is a step 12/16 decision (see the step 9 status note).
RUN_SCOPED = frozenset(SANDBOX_TOOLS)
