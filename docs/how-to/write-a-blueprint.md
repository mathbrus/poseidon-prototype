# Write your blueprint

A blueprint is the one YAML file that defines a company: its vision, its
repository, who may act on it, its budget and its merge rules. This
guide writes one, checks it, and applies it.

## Before you start

- A running cluster, as in the [Quickstart](../get-started/quickstart.md).
- A disposable GitHub repository your `GIT_TOKEN` can write to.

## 1. Answer the questions

```bash
uv run poseidon blueprint new
```

The command asks for what is yours to decide:

- the company's name and vision,
- who may act on it,
- the repository and its test command,
- the hosts the sandbox may reach,
- whether routine changes merge without you, and in which paths,
- the budgets.

Every question has the shipped example's answer as its default, so you can
press Enter through all but the vision and the repository. The command
checks that the answers compile, then writes `<company-name>.yaml` in the
current directory. It sends nothing to the cluster.

To start from a file instead, copy `examples/hello-saas.yaml` and edit it.
The [blueprint reference](../reference/blueprint.md) describes every field.

## 2. Preview the change

```bash
uv run poseidon blueprint preview my-company.yaml
```

`preview` compiles the blueprint and shows what applying it would change:
the agents it would create or remove, and every change of authority. It
changes nothing.

## 3. Apply it

```bash
uv run poseidon blueprint apply my-company.yaml --yes
```

`apply` shows the same diff, applies the blueprint, and waits until the
company's policy is live (`converged`). Without `--yes`, it asks first.

To change the company later, edit the file and apply it again. The
blueprint is the only configuration a company has.

## Fix a blueprint that doesn't compile

The console checks a blueprint before anything reaches the cluster. These
are the mistakes people make most often, with the message you see:

| Mistake | Message |
|---|---|
| A misspelled key | `company: Additional properties are not allowed ('visoin' was unexpected)` |
| No `heartbeat` section | `(document root): 'heartbeat' is a required property` |
| A standing agent that writes code | `standing.coder: standing count 1 on non-spawner role 'implementer' — execution is ephemeral crews` |
| Giving a role a tool its catalog entry lacks | `tools.reviewer.allow: 'git.open_pr' is not in the 'reviewer' role's catalog tools — the blueprint narrows the catalog, never widens it` |
| A workflow's crew missing from the PM's `may_spawn` | `workflows.review: conductor 'pm' may not spawn 'feature-review' — add it to may_spawn.crew` |
| Automating a protected path | `workflows.review.gates.merge.automate.paths: '.github/' overlaps '.github/', which is never automated` |
| A daily cap smaller than one run's budget | `heartbeat.daily_cap.usd: 1 cannot fund a single run (budgets.per_run.usd is 5)` |

## Next steps

- [Blueprint reference](../reference/blueprint.md): every field
- [Automate merges](automate-merges.md)
- [Use a paid brain](paid-brain.md)
