package poseidon.poc_test

import rego.v1

import data.poseidon.poc

assignments := {"backend-engineer-1": ["run-abc", "run-def"]}

engineer_input(tool, run_id, args) := {
	"agent_id": "backend-engineer-1",
	"role": "backend-engineer",
	"run_id": run_id,
	"tool": tool,
	"args": args,
}

runner_input(tool, args) := {
	"agent_id": "runner",
	"role": "runner",
	"run_id": "run-abc",
	"tool": tool,
	"args": args,
}

# --- default deny ---------------------------------------------------------

test_default_deny_unknown_tool if {
	d := poc.decision with input as engineer_input("something.else", "run-abc", {})
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "default_deny"}
}

# --- sandbox.* / run assignment -------------------------------------------

test_sandbox_allowed_for_assigned_run if {
	d := poc.decision with input as engineer_input("sandbox.exec", "run-abc", {"cmd": "ls"})
		with data.runs.assignments as assignments
	d == {"allow": true, "rule": "sandbox_assigned_run"}
}

test_sandbox_all_tools_allowed_for_assigned_run if {
	every tool in {
		"sandbox.read_file", "sandbox.write_file", "sandbox.list_dir",
		"sandbox.exec", "sandbox.run_tests", "sandbox.git_bundle",
	} {
		d := poc.decision with input as engineer_input(tool, "run-def", {})
			with data.runs.assignments as assignments
		d.allow == true
	}
}

test_sandbox_denied_for_unassigned_run if {
	d := poc.decision with input as engineer_input("sandbox.exec", "run-other", {"cmd": "ls"})
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "default_deny"}
}

test_sandbox_denied_for_unknown_agent if {
	d := poc.decision with input as {
		"agent_id": "backend-engineer-2",
		"role": "backend-engineer",
		"run_id": "run-abc",
		"tool": "sandbox.exec",
		"args": {},
	}
		with data.runs.assignments as assignments
	d.allow == false
}

test_sandbox_denied_for_wrong_role if {
	d := poc.decision with input as runner_input("sandbox.exec", {"cmd": "ls"})
		with data.runs.assignments as assignments
	d.allow == false
}

test_sandbox_denied_with_no_assignments_data if {
	d := poc.decision with input as engineer_input("sandbox.exec", "run-abc", {})
		with data.runs.assignments as {}
	d.allow == false
}

# --- git.open_pr / branch rules --------------------------------------------

test_open_pr_allowed_on_feature_branch if {
	d := poc.decision with input as engineer_input(
		"git.open_pr", "run-abc",
		{"branch": "feature/add-endpoint-2", "paths": ["src/app.py", "tests/test_app.py"]},
	)
		with data.runs.assignments as assignments
	d == {"allow": true, "rule": "git_open_pr_feature_branch"}
}

test_open_pr_denied_on_main if {
	d := poc.decision with input as engineer_input("git.open_pr", "run-abc", {"branch": "main"})
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "git_open_pr_protected_branch"}
}

test_open_pr_denied_on_master if {
	d := poc.decision with input as engineer_input("git.open_pr", "run-abc", {"branch": "master"})
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "git_open_pr_protected_branch"}
}

test_open_pr_denied_on_main_case_insensitive if {
	d := poc.decision with input as engineer_input("git.open_pr", "run-abc", {"branch": "Main"})
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "git_open_pr_protected_branch"}
}

test_open_pr_denied_on_nonconforming_branch if {
	d := poc.decision with input as engineer_input("git.open_pr", "run-abc", {"branch": "feature/Bad_Name"})
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "git_open_pr_branch_pattern"}
}

test_open_pr_denied_on_feature_prefix_trick if {
	d := poc.decision with input as engineer_input("git.open_pr", "run-abc", {"branch": "feature/x/../main"})
		with data.runs.assignments as assignments
	d.allow == false
}

test_open_pr_denied_when_branch_missing if {
	d := poc.decision with input as engineer_input("git.open_pr", "run-abc", {})
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "git_open_pr_branch_pattern"}
}

# --- git.open_pr / workflow path guard --------------------------------------

test_open_pr_denied_when_touching_workflows if {
	d := poc.decision with input as engineer_input(
		"git.open_pr", "run-abc",
		{"branch": "feature/sneaky", "paths": ["src/app.py", ".github/workflows/ci.yml"]},
	)
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "git_open_pr_workflow_paths"}
}

test_open_pr_allowed_when_paths_outside_workflows if {
	d := poc.decision with input as engineer_input(
		"git.open_pr", "run-abc",
		{"branch": "feature/ok", "paths": [".github/dependabot.yml", "src/app.py"]},
	)
		with data.runs.assignments as assignments
	d.allow == true
}

# --- repo.archive ------------------------------------------------------------

test_repo_archive_allowed_for_runner if {
	d := poc.decision with input as runner_input("repo.archive", {"repo": "org/repo", "ref": "main"})
		with data.runs.assignments as assignments
	d == {"allow": true, "rule": "repo_archive_runner"}
}

test_repo_archive_denied_for_engineer if {
	d := poc.decision with input as engineer_input("repo.archive", "run-abc", {"repo": "org/repo", "ref": "main"})
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "default_deny"}
}

# --- /admin/sandboxes ---------------------------------------------------------

test_admin_sandboxes_allowed_for_runner if {
	d := poc.decision with input as runner_input("/admin/sandboxes", {"run_id": "run-abc", "url": "http://sbx:8000"})
		with data.runs.assignments as assignments
	d == {"allow": true, "rule": "admin_sandboxes_runner"}
}

test_admin_sandboxes_denied_for_engineer if {
	d := poc.decision with input as engineer_input("/admin/sandboxes", "run-abc", {"run_id": "run-abc", "url": "http://evil:8000"})
		with data.runs.assignments as assignments
	d == {"allow": false, "rule": "default_deny"}
}
