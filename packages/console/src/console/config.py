"""Console settings. Inherits the shared verification shape
(`common.authn`) — the console verifies against its own audience — and
adds where policy and workflows live, and who the humans are on a
cluster without an identity provider. Which company the cluster hosts is
not a setting: the console reads it from the registry, where the
reconciler records what was applied.
"""

from common.authn import AuthnSettings


class ConsoleSettings(AuthnSettings):
    """Every field is set by the chart's console template."""

    opa_url: str
    temporal_address: str
    # The humans the chart declared as `human-<name>` ServiceAccounts,
    # comma-separated — the only ServiceAccount principals the console
    # maps to a person. An identity provider's users need no declaring.
    console_humans: str = ""

    @property
    def humans(self) -> list[str]:
        return [name.strip() for name in self.console_humans.split(",") if name.strip()]
