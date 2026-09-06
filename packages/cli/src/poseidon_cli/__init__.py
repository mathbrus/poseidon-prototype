"""The `poseidon` CLI, and the console client beneath it.

The CLI is a thin HTTP client of the console: it holds a human's token
(`POSEIDON_TOKEN`) and the console's address (`POSEIDON_CONSOLE_URL`), and
performs no act itself — every act is the console's, decided and audited
there under the person's name. `ConsoleClient` is the same client as a
library, for the acceptance suite and any harness that acts on a company.

The one thing the CLI does with cluster credentials is `poseidon login`:
minting the token, through the TokenRequest API with the caller's own
kubeconfig, for a human the chart declared.
"""

from poseidon_cli.client import ConsoleClient, ConsoleError, PolicyDenied

__all__ = ["ConsoleClient", "ConsoleError", "PolicyDenied"]
