# mockllm

A deterministic, OpenAI-compatible brain that stands in for the LLM proxy so
end-to-end runs cost nothing. **It is the default in every development and
test path**, and that is a deliberate cost guardrail, not a convenience:
the full 91-test kind acceptance suite runs the real agent, the real
gateways, the real policy engine and a real cluster, and spends zero tokens.

Using a paid provider instead requires bringing the cluster up with
`REAL_LLM=1` explicitly. Nothing does that by default, and CI never does.

## Dev/test only

This is not a production component. It impersonates the LiteLLM proxy — same
service name, same port, same `/chat/completions` route, same
`/health/liveliness` probe the `llm-proxy` Deployment expects — so the
`values-mockllm.yaml` swap is a pod-template change and nothing else.

Requests are not authenticated. That matches the real proxy, which ignores
the agent's placeholder bearer token too, and neither service is reachable
from outside the platform namespace: the metered `gateway-llm` is the only
thing NetworkPolicy lets talk to either of them.

## How the brain decides what to say

`brain.py` is a pure function of the conversation the host sends. It keys on
the **offered tool surface** (with the SDK's own `finish`/`think` control
tools filtered out) plus the system prompt, which is exactly how the real
dispatch modes differ:

| Offered | Phase | Scripted behaviour |
|---|---|---|
| no tools | planner | the task brief, echoing the branch line and scenario markers |
| tools without `open_pr` | act | write `slugify.py` + its test once, then answer in plain text — which ends the conversation so the host runs the advisory verify |
| tools with `open_pr` | deliver | commit on the feature branch, open the PR |
| issue/crew tools | PM | file one ticket, record the crew request, summarize |
| review tools | reviewer | read the PR diff, run the suite, post one findings comment |

Because the brief *replaces* the task text for the executor, the planner
script must echo the branch line and any scenario marker into it — a marker
that is not echoed is simply lost. That is a real property of the handoff,
and the mock is written to exercise it rather than paper over it.

## The scenarios it scripts

Driven by the files in `examples/`:

- `task-happy.md` — implement, verify green, deliver a PR.
- `task-verify-red.md` — plants a failing test so the platform verify gate
  goes red, then removes the plant on the workflow's feedback re-dispatch,
  exercising the bounded retry loop.
- `task-injected.md` — a prompt-injection payload in the task text; the
  brain obeys it credulously, which is the point: the platform has to be
  what stops it.
- `goal-injected-conductor.md` — a poisoned intake goal telling the PM to
  spawn a crew outside its `may_spawn` list and the delivery crew to publish
  the workspace to an outside collector. The exfiltration uses `urllib`
  rather than `curl` on purpose: the sandbox image has no curl, so a curl
  payload would die at "command not found" without the egress layer ever
  seeing it — and the whole point is that the audited proxy is what stops it.

**A credulous brain is the correct brain for these tests.** Every scenario
asserts that compiled policy, the egress proxy or the merge gate refuses the
attempt — never that the model declined.

## Entry points

```
python -m mockllm --model <ignored> --port 4000
```

`Dockerfile.mockllm`. The LiteLLM-shaped args are accepted and ignored so
the Helm template's `args` work verbatim whether the pod runs LiteLLM or
this.

## Cross-package interactions

- Sits behind `gateway-llm`, which meters and budget-gates it exactly as it
  would a real provider. Token usage is a deterministic estimate
  (~4 chars/token) — nonzero so metering and the budget hard-stop have real
  usage to account, stable so e2e assertions can rely on it.
- Drives `agent.brain`'s production SDK loops.

## Tests

```
uv run pytest packages/mockllm
```

`test_contract.py` is the valuable one: it runs the **real brain host**
(`agent.brain`) over real HTTP against this server, proving the whole wire
contract — routes, response shape, tool-call JSON, the SDK's litellm
transport — without a cluster or a paid token. It is the cheapest place a
brain-host regression surfaces.

## Invariants

- Deterministic: the same conversation always produces the same completion.
  An e2e failure means the platform changed, not that the model felt
  different today.
- Content is stamped per delivery branch. With byte-identical content the
  first merge would make every later delivery an empty diff, and
  `git.open_pr` would have no commits to open a PR from — which quietly
  poisons the disposable test repo.
