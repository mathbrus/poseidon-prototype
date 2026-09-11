"""The OPA bundle server — the bundle delivery contract.

OPA polls this for each company's compiled bundle; nothing is ever pushed
into a running OPA. That is what makes gateway and OPA restarts safe — a
restarted OPA re-fetches the current revision instead of running on whatever
happened to be in its memory — and it keeps apply the single mutation point
for enforcement.

Standard bundle semantics: `ETag` carries the revision and a matching
`If-None-Match` gets 304, so a steady state costs one cheap request per poll
interval rather than a tarball.
"""

from fastapi import FastAPI, Header, Response

from common.topology import BUNDLE_PORT
from registry import PolicyBundles, open_registry


def create_app(bundles: PolicyBundles | None = None) -> FastAPI:
    store: PolicyBundles | None = bundles
    app = FastAPI(title="poseidon-bundle-server")

    def _store() -> PolicyBundles:
        # Lazy on purpose: on a fresh cluster this process can start before
        # Postgres accepts connections, and a connect at create_app() time
        # would crash-loop it. /healthz stays database-free — readiness is
        # about the process; a warming-up store surfaces as a 500 on the
        # bundle route, which OPA retries on its own poll schedule.
        nonlocal store
        if store is None:
            store = PolicyBundles(open_registry())
        return store

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/bundles/{company}/bundle.tar.gz")
    def bundle(
        company: str, if_none_match: str | None = Header(default=None)
    ) -> Response:
        stored = _store().get(company)
        if stored is None:
            # Nothing was ever stored under this name. A torn-down company
            # is not this case: teardown leaves a tombstone, served like any
            # revision, because OPA keeps serving what it has on a 404 —
            # revocation has to be a bundle it can activate.
            return Response(status_code=404)
        etag = f'"{stored.revision}"'
        if if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(
            content=stored.tarball,
            media_type="application/gzip",
            headers={"ETag": etag},
        )

    return app


def main() -> None:
    """Build the app here rather than at import: connecting to the registry
    as a side effect of importing the module would make it untestable and
    unimportable without a database."""
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=BUNDLE_PORT)


if __name__ == "__main__":
    main()
