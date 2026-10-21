# Act as another person

Every `poseidon` command runs as a person, and the console records each
act under that person's name. On kind, the chart declares two people,
`owner` and `auditor`, and `kind-up.sh` signs you in as `owner`. This
guide switches to another person, for example to check what someone
the blueprint doesn't name can do.

## Before you start

- A running kind cluster and the kubeconfig `kind-up.sh` set up.

## Steps

Mint a token for the other person and export it. The kubeconfig you
already hold is what allows you to mint it:

```bash
export POSEIDON_TOKEN=$(uv run poseidon login auditor)
```

The token lasts one hour. The `POSEIDON_TOKEN` variable takes precedence
over the token `kind-up.sh` saved for you.

## Check that it worked

```bash
uv run poseidon whoami
```

```text
you are auditor (human)
company: hello-saas · converged
```

## Undo it

```bash
unset POSEIDON_TOKEN
```

The CLI goes back to the token saved in `~/.config/poseidon/token`.

## Who may do what

Being declared by the chart lets a person sign in. Being named in the
blueprint's `company.humans` lets them act. A person the blueprint doesn't
name can read the company but every act is refused with
`human_not_recognized`, under their name.

To give someone the right to act, add them to `company.humans` and apply
the blueprint again. To take it away, remove them and apply again.

To declare more people on kind, add them to `console.humans` in
`deploy/helm/poseidon/values-kind.yaml` and run `kind-up.sh` again.
