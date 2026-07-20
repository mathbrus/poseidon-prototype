"""Single-line JSON logging to stdout (contract C8).

`get_logger(name)` returns a logger whose records render as one JSON object
per line. Context such as run_id/agent_id is attached with `.bind(...)`,
which returns a new logger carrying those fields on every line.

This module is the seam for swapping in the OTel SDK later.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "bound", {}))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class BoundLogger(logging.LoggerAdapter):
    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        fields = dict(self.extra or {})
        fields.update(kwargs.pop("extra", None) or {})
        kwargs["extra"] = {"bound": fields}
        return msg, kwargs

    def bind(self, **fields: Any) -> "BoundLogger":
        return BoundLogger(self.logger, {**(self.extra or {}), **fields})


def _ensure_configured() -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def get_logger(name: str, **bound: Any) -> BoundLogger:
    _ensure_configured()
    return BoundLogger(logging.getLogger(name), bound)
