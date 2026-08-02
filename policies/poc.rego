# Poseidon PoC policy — contract C2.
#
# Input:  {"agent_id": str, "role": str, "run_id": str, "tool": str, "args": {...}}
# Query:  POST /v1/data/poseidon/poc/decision -> {"result": {"allow": bool, "rule": str}}
#
# Run assignments are pushed by the gateway via OPA's data API into
# data.runs.assignments: {<agent_id>: [<run_id>, ...]}.
#
# Tool naming: MCP tools use their MCP names (e.g. "sandbox.exec", "git.open_pr");
# the gateway admin endpoint is policed under the tool name "/admin/sandboxes".
package poseidon.poc

import rego.v1

default decision := {"allow": false, "rule": "default_deny"}

# Explicit denies veto everything; otherwise any matching allow rule grants.
decision := {"allow": false, "rule": min(deny_rules)} if {
	count(deny_rules) > 0
}

decision := {"allow": true, "rule": min(allow_rules)} if {
	count(deny_rules) == 0
	count(allow_rules) > 0
}

# --- allow rules ---------------------------------------------------------

# backend-engineer may call sandbox.* only for a run currently assigned to it.
allow_rules contains "sandbox_assigned_run" if {
	input.role == "backend-engineer"
	startswith(input.tool, "sandbox.")
	input.run_id in data.runs.assignments[input.agent_id]
}

# git.open_pr only onto feature branches (deny rules below can still veto).
allow_rules contains "git_open_pr_feature_branch" if {
	input.tool == "git.open_pr"
	regex.match(`^feature/[a-z0-9-]+$`, pr_branch)
}

# repo.archive is reserved for the runner.
allow_rules contains "repo_archive_runner" if {
	input.tool == "repo.archive"
	input.role == "runner"
}

# Registering and retiring a sandbox address (gateway admin endpoints) is
# reserved for the runner.
allow_rules contains "admin_sandboxes_runner" if {
	input.tool in {"/admin/sandboxes", "/admin/sandboxes.delete"}
	input.role == "runner"
}

# --- deny rules ----------------------------------------------------------

deny_rules contains "git_open_pr_protected_branch" if {
	input.tool == "git.open_pr"
	protected_branch
}

deny_rules contains "git_open_pr_branch_pattern" if {
	input.tool == "git.open_pr"
	not protected_branch
	not regex.match(`^feature/[a-z0-9-]+$`, pr_branch)
}

# Argument-level semantic guard: refuse PRs whose changed paths touch CI workflows.
deny_rules contains "git_open_pr_workflow_paths" if {
	input.tool == "git.open_pr"
	some path in object.get(input, ["args", "paths"], [])
	startswith(path, ".github/workflows/")
}

# --- helpers -------------------------------------------------------------

pr_branch := object.get(input, ["args", "branch"], "")

protected_branch if lower(pr_branch) in {"main", "master"}
