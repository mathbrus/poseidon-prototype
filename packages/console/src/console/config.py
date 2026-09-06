"""Console settings. Inherits the shared verification shape
(`common.authn`) — the console verifies against its own audience — and
adds where the company's policy and workflows live, and who the humans
are on a cluster without an identity provider.
"""

from common.authn import WorkloadAuthnSettings


class ConsoleSettings(WorkloadAuthnSettings):
    """Every field is set by the chart's console template."""

    # The one company this installation hosts: the bundle its decisions
    # are evaluated against, the workflows it starts and signals.
    company: str
    opa_url: str
    temporal_address: str
    # The humans the chart declared as `human-<name>` ServiceAccounts,
    # comma-separated — the only ServiceAccount principals the console
    # maps to a person. An identity provider's users need no declaring.
    console_humans: str = ""

    @property
    def humans(self) -> list[str]:
        return [name.strip() for name in self.console_humans.split(",") if name.strip()]
