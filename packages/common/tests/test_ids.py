import re

from common.ids import new_run_id

# ULID canonical form: 26 chars of Crockford base32
RUN_ID_RE = re.compile(r"^run-[0-9A-HJKMNP-TV-Z]{26}$")


def test_format():
    assert RUN_ID_RE.match(new_run_id())


def test_unique():
    ids = {new_run_id() for _ in range(1000)}
    assert len(ids) == 1000
