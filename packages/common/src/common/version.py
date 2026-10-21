"""The version of the Poseidon this process is part of.

Images carry it: `deploy/build-images.sh` stamps the tree's `git
describe` into `POSEIDON_VERSION` at build time, so a release's images
answer the tag's name (`v0.1.0+2026.10.21`) and a build between releases
answers the tag plus its distance and commit (`v0.1.0+2026.10.21-5-gabc1234`,
`-dirty` for an uncommitted tree). A process built outside git, or run
from a checkout, answers `unreleased`. The console reports it on its
health route, `poseidon version` reads it there, and `kind-up` prints it
as the last thing it says.
"""

import os

VERSION_VAR = "POSEIDON_VERSION"
UNRELEASED = "unreleased"


def running_version() -> str:
    return os.environ.get(VERSION_VAR, "").strip() or UNRELEASED
