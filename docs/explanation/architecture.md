# Architecture

Two paths run through the system. The **declarative path** turns a document
into enforcement; the **runtime path** does work under that enforcement.

## The declarative path

```mermaid
flowchart LR
    H(["a human<br/><i>poseidon blueprint apply</i>"]) --> CON["<b>console</b><br/>verify · decide · audit"]
    CON --> BP["document<br/><i>hello-saas.yaml</i>"]
    BP --> SCH["<b>blueprint</b><br/>well-formed?"]
    SCH --> CMP["<b>compiler</b><br/>coherent?<br/>pure function"]
    CMP --> PLAN["deployment plan<br/>CRs · Rego · infra IR"]
    PLAN --> OP["<b>reconciler</b><br/>converges"]
    OP --> NS["namespace<br/>NetworkPolicies<br/>Deployments<br/>RBAC"]
    OP --> REG[("<b>registry</b><br/>Postgres")]
    REG --> BS["<b>bundle server</b>"]
    BS -.OPA polls.-> OPA["<b>OPA</b>"]
```

The split between `blueprint` and `compiler` is worth understanding. `blueprint`
asks *is this document well-formed?* — a question answerable from the
document alone. `compiler` asks *is this company coherent?* — is the
heartbeat's conductor an active standing spawner, does every scope name a
real tool, can the daily cap fund a run. Both fail before anything reaches
a cluster.

Note the dotted arrow. Policy is **never pushed** into OPA. The reconciler
persists a bundle; OPA polls for it. That is what makes an OPA restart safe:
it re-fetches the current revision rather than running on whatever it last
held in memory.

## The runtime path

```mermaid
flowchart TB
    subgraph company["company namespace"]
        PM["<b>PM</b><br/>standing agent"]
        EX["<b>implementer</b><br/>ephemeral agent"]
        SBX["<b>sandbox</b><br/>K8s Job · untrusted code"]
    end
    subgraph platform["platform namespace"]
        ORC["<b>orchestrator</b><br/>Temporal workflows"]
        GW["<b>MCP gateway</b><br/>holds GIT_TOKEN"]
        LGW["<b>LLM gateway</b><br/>meters + budgets"]
        PROXY["<b>egress proxy</b>"]
        OPA2["<b>OPA</b>"]
        DB[("<b>registry</b>")]
        CON["<b>console</b><br/>the human's door"]
        TMP["<b>Temporal</b><br/>fenced"]
    end
    HUM(["a human"]) -->|"verdicts, applies, reads"| CON
    CON -->|decision| OPA2
    CON -->|"audit, under a name"| DB
    CON -->|"start · signal · inspect"| TMP
    ORC --- TMP
    ORC -->|dispatch| PM
    ORC -->|dispatch| EX
    PM -->|tools| GW
    EX -->|tools| GW
    GW -->|decision| OPA2
    GW -->|audit + binding| DB
    GW -->|proxied| SBX
    EX -->|completions| LGW
    LGW --> DB
    SBX -->|"only route out"| PROXY
    PROXY --> DB
    PROXY -->|allowlist only| INET(["internet"])
```

Read the arrows into `registry` as "the whole story of a run ends up here."
One `run_id` query returns the gateway's decisions, the proxy's allows and
denies, the LLM gateway's spend, the process gates' verdicts and the
human's acts, in order. Six distinct sources write audit events —
`gateway`, `llm-gateway`, `sandbox-infra`, `process-gate`,
`process-lifecycle`, `console` — and the acceptance suite asserts that a
single run's story is reconstructible across all six from the `run_id`
alone ([Policy and audit](../reference/policy.md) lists them). The console's rows are
the only ones carrying a person's name; the merge gate's release reads
one of them back.

## What each service is responsible for

| Service | Holds | Responsible for |
|---|---|---|
| **reconciler** | — | Reconciling CRs into namespaces, NetworkPolicies, Deployments, RBAC; persisting bundles. Never runs a process, never creates an ephemeral agent. |
| **orchestrator** | egress HMAC key, registry DSN | Temporal workflows, activity sequencing, ephemeral pods and their per-run ServiceAccounts, identity binding, the merge gate's park and its release on the recorded verdict. No `GIT_TOKEN`, no signing key. |
| **console** | registry DSN | Every human act: authenticate the person, decide, audit under their name, act — apply the blueprint (it alone submits the CRs), record and deliver gate verdicts, start or cancel a run, wake, pause or resume the heartbeat, delete the company; and every human read. |
| **MCP gateway** | `GIT_TOKEN` | Every tool call: authenticate, decide, audit, act. |
| **LLM gateway** | — | Every model call: authenticate, attribute to a run, check budget, pin the deployment's model, meter. |
| **llm-proxy** | provider API key | Talking to the actual model provider. Reachable *only* from the LLM gateway. |
| **egress proxy** | egress HMAC key | The one network route out of a sandbox; allowlist + audit. |
| **OPA** | — | Answering decision queries from compiled bundles. |
| **registry** (Postgres) | — | Nine tables, one writer each; the audit and run store. |
| **agent** | *nothing* | Running the procedure a dispatch names, against the model behind the LLM gateway. Its credential is its own ServiceAccount's projected token file — for standing agents and ephemeral agents alike. |
| **sandbox** | *nothing* | Running untrusted workspace code. |

Each service is one workspace package; [Packages](../developing/packages.md)
maps them to their READMEs.
