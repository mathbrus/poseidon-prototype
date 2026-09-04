"""The gateway tool surface a blueprint may name.

The compiler checks every declared scope against this list ("every
tool scope names a registered gateway backend"), so a typo fails at compile
instead of at the first tool call.
"""

SANDBOX_TOOLS = [
    "sandbox.read_file",
    "sandbox.write_file",
    "sandbox.edit_file",
    "sandbox.list_dir",
    "sandbox.exec",
    "sandbox.run_tests",
    "sandbox.git_bundle",
]
REPO_TOOLS = ["repo.archive"]
GIT_TOOLS = ["git.open_pr"]

ISSUE_TOOLS = ["issues.create", "issues.read", "issues.comment"]
REVIEW_TOOLS = ["git.read", "git.comment"]
# A crew member's one way to change the architecture masterplan: record a
# proposal, which the platform files as a stewardship ticket. Asking is
# not writing.
ARCHITECTURE_TOOLS = ["architecture.request"]
# Platform lifecycle only: the merge the orchestrator performs after the
# gate, and the read of the company's documents at `base_ref` before every
# tick and every run — never the sandbox's copy.
PLATFORM_TOOLS = ["git.merge_pr", "repo.read_file"]

KNOWN_TOOLS = frozenset(
    SANDBOX_TOOLS
    + REPO_TOOLS
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
