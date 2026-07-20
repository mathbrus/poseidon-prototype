from ulid import ULID


def new_run_id() -> str:
    return f"run-{ULID()}"
