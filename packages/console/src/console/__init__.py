"""The console: the human's chokepoint, the one service a person acts on
a company through.

The counterpart of the MCP gateway for agents. Every request is a verified
human (a Kubernetes-issued token for the console's own audience, mapped
by `common.authn`); every act — the console's rows in
`common.audit.PLATFORM_EVENTS`: apply the blueprint, resolve a gate,
start or cancel a run, wake, pause or resume the heartbeat, delete the
company — is a decision the console must win against the company's
compiled bundle before it does anything, and is audited under the
person's name. Reads (a run, its story, its spend, the heartbeat, the
company) require the same verified human and touch nothing.

Layout:
    config.py    settings: OPA, Temporal, the declared humans — never the
                 company, which is read from the registry
    service.py   the acts and the reads — verify, decide, do, audit
    temporal.py  the Temporal client: start, signal, cancel, inspect
    crs.py       the cluster touch: submit the Company and AgentInstance
                 CRs, read the Company's phase, delete the Company
    app.py       the FastAPI app: routes, the shared auth middleware

Run with: uvicorn --factory console.app:create_app
"""
