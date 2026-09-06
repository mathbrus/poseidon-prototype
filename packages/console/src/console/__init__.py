"""The console: the one door a person acts on a company through.

The counterpart of the MCP gateway for agents. Every request is a verified
human (a Kubernetes-issued token for the console's own audience, mapped
by `common.authn`); every act — apply the blueprint, resolve a gate, start
or cancel a run, wake the heartbeat — is a decision the console must win
against the company's compiled bundle before it does anything, and is
audited under the person's name. Reads (a run, its story, its spend, the
heartbeat) require the same verified human and touch nothing.

Layout:
    config.py    settings: the company, OPA, Temporal, the declared humans
    service.py   the acts and the reads — verify, decide, do, audit
    temporal.py  the Temporal client: start, signal, cancel, inspect
    crs.py       the blueprint apply: the Company and AgentInstance CRs
    app.py       the FastAPI app: routes, the shared auth middleware

Run with: uvicorn --factory console.app:create_app
"""
