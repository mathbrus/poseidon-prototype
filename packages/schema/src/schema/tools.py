"""The gateway tool surface a blueprint may name.

The compiler checks every declared scope against this list ("every
tool scope names a registered gateway backend"), so a typo fails at compile
instead of at the first tool call.
"""

# The sandbox tools that read, and the ones that write — named apart
# because a blueprint's reviewer scope is exactly "the role's catalog
# minus the writes": it judges, it does not fix.
SANDBOX_READ_TOOLS = [
    "sandbox.read_file",
    "sandbox.list_dir",
    "sandbox.exec",
    "sandbox.run_tests",
]
SANDBOX_WRITE_TOOLS = [
    "sandbox.write_file",
    "sandbox.edit_file",
    "sandbox.git_bundle",
]
SANDBOX_TOOLS = SANDBOX_READ_TOOLS + SANDBOX_WRITE_TOOLS
GIT_TOOLS = ["git.open_pr"]

ISSUE_TOOLS = ["issues.create", "issues.read", "issues.comment"]
REVIEW_TOOLS = ["git.read", "git.comment"]
# A crew member's one way to change the architecture masterplan: record a
# proposal, which the platform files as a stewardship ticket. Asking is
# not writing.
ARCHITECTURE_REQUEST = "architecture.request"
ARCHITECTURE_TOOLS = [ARCHITECTURE_REQUEST]
# The tools whose call commits the company to something in the world — a
# ticket, a pull request, a request to change the architecture — take a
# `rationale`: the caller's own reason, recorded in cleartext on the
# call's audit row and read back in the run's story. Every other row
# carries a digest and nothing more.
RATIONALE_TOOLS = frozenset(["issues.create", "git.open_pr", ARCHITECTURE_REQUEST])
# Platform lifecycle only: the archive a run's sandbox is provisioned
# from, the merge the orchestrator performs after the gate, and the read
# of the company's documents at `base_ref` before every tick and every
# run — never the sandbox's copy.
PLATFORM_TOOLS = ["repo.archive", "git.merge_pr", "repo.read_file"]

KNOWN_TOOLS = frozenset(
    SANDBOX_TOOLS
    + GIT_TOOLS
    + ISSUE_TOOLS
    + REVIEW_TOOLS
    + ARCHITECTURE_TOOLS
    + PLATFORM_TOOLS
)

# Tools no company role may declare: the platform identity holds them, and a
# blueprint that could grant them would make the merge gate optional.
PLATFORM_ONLY = frozenset(PLATFORM_TOOLS)

# Tools whose compiled rules additionally require a verified run binding in
# the decision input — a role allow-list without run binding reopens
# cross-run tool use. A property of the tool, not a name prefix.
RUN_SCOPED = frozenset(SANDBOX_TOOLS + ARCHITECTURE_TOOLS)
