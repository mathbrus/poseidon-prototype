# Run the acceptance suite

```bash
scripts/acceptance.sh
```

The full end-to-end suite, under an hour, against the live cluster (the
exact count is in the pytest summary). It opens real pull requests on your
disposable repo — and owns that repo's state entirely: it seeds `main` to
a declared baseline, closes every PR and branch it creates, and finishes
by *asserting* the cluster is quiet, so two consecutive runs are green
with nothing done between them and a fresh disposable repo needs no
manual setup (`tests/e2e_kind/conftest.py` documents the contract). This
is the suite that proves the claims in [the argument](../argument/can.md)
— including the red-team modules, which attack a real provisioned sandbox
and a real compiled merge gate.

It spends nothing: the suite refuses to run against a paid brain unless
`POSEIDON_ALLOW_PAID=1` opts in ([Use a real model](real-llm.md)).

```bash
deploy/kind/kind-down.sh    # when you are done
```
