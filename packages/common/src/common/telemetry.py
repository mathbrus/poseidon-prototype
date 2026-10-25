"""Single-line JSON logging to stdout — and, when configured, the same
records exported over OTLP.

`get_logger(name)` returns a logger whose records render as one JSON object
per line. Context such as run_id/agent_id is attached with `.bind(...)`,
which returns a new logger carrying those fields on every line.

OTel enters at this seam and nowhere else:

- stdout JSON stays the always-on contract — `kubectl logs` and every
  debugging workflow keep working unchanged, exporter or not;
- when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (the standard SDK variable),
  an OTLP log exporter is attached alongside it: bound fields become
  log-record attributes, `OTEL_SERVICE_NAME` names the service, and an
  active span's trace context rides along automatically;
- the SDK is an optional extra (`common[otel]`) so the sandbox toolserver
  image — which runs untrusted workspace code — never carries it.
  Platform services depend on the extra;
- telemetry must never take a service down: if the endpoint is set but
  the SDK is missing, one warning line says so and stdout-only continues.
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

# The attributes every LogRecord carries; anything else on a record is an
# extra its caller attached.
_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
# Python's level names that OTel spells differently.
_SEVERITY_TEXT = {"WARNING": "WARN", "CRITICAL": "FATAL"}


def _attributes(record: logging.LogRecord) -> dict[str, Any]:
    """A record's extras as OTLP attributes, the `bound` dict (run_id,
    agent_id, …) flattened into them — OTLP attributes must be primitives,
    so a non-primitive value is stringified and the dict itself never
    rides through — and where the record was logged from."""
    extras = {k: v for k, v in vars(record).items() if k not in _RECORD_ATTRS}
    extras.update(extras.pop("bound", None) or {})
    attributes = {
        key: value if isinstance(value, (bool, int, float, str, bytes)) else str(value)
        for key, value in extras.items()
    }
    attributes["code.file.path"] = record.pathname
    attributes["code.function.name"] = record.funcName
    attributes["code.line.number"] = record.lineno
    return attributes


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
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


def _otel_handler(exporter: Any | None = None) -> logging.Handler | None:
    """The OTLP bridge, built lazily and only when configured. Returns None
    (after one warning line) rather than raising: an unreachable collector
    or a missing extra must degrade to stdout-only, never crash a service
    at import time. `exporter` is a test seam; production always builds
    the OTLP exporter from the standard environment variables."""
    try:
        # SDK versions spell the logs package with and without the
        # underscore; accept either so a version bump in uv.lock cannot
        # silently disable export.
        try:
            from opentelemetry.sdk.logs import LoggerProvider
            from opentelemetry.sdk.logs.export import BatchLogRecordProcessor
        except ImportError:
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        try:
            from opentelemetry.exporter.otlp.proto.http.log_exporter import (
                OTLPLogExporter,
            )
        except ImportError:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                OTLPLogExporter,
            )
        from opentelemetry._logs import SeverityNumber
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logging.getLogger(__name__).warning(
            "%s is set but the OTel SDK is not installed"
            " (common[otel]); continuing stdout-only",
            OTLP_ENDPOINT_ENV,
        )
        return None

    def severity(levelno: int) -> Any:
        # Python's five levels onto OTel's: DEBUG 5, INFO 9, WARN 13,
        # ERROR 17, FATAL 21.
        if levelno < logging.DEBUG:
            return SeverityNumber.UNSPECIFIED
        return SeverityNumber(min(levelno // 10, 5) * 4 + 1)

    class OtlpHandler(logging.Handler):
        """Each record emitted on the provider as an OTel log record: the
        message as the body, the extras as attributes (`_attributes`), an
        exception's type, message and stack when the record carries one,
        and the active span's context, which the record picks up itself."""

        def __init__(self, provider: Any) -> None:
            super().__init__()
            self.provider = provider

        def emit(self, record: logging.LogRecord) -> None:
            try:
                self.provider.get_logger(record.name).emit(
                    timestamp=int(record.created * 1e9),
                    severity_number=severity(record.levelno),
                    severity_text=_SEVERITY_TEXT.get(record.levelname, record.levelname),
                    body=record.getMessage(),
                    attributes=_attributes(record),
                    exception=record.exc_info[1] if record.exc_info else None,
                )
            except Exception:
                self.handleError(record)

    # Resource.create() honors OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES;
    # the provider registers its own atexit shutdown, which flushes the
    # batch processor.
    provider = LoggerProvider(resource=Resource.create())
    provider.add_log_record_processor(
        BatchLogRecordProcessor(exporter if exporter is not None else OTLPLogExporter())
    )
    return OtlpHandler(provider)


def _ensure_configured() -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if os.environ.get(OTLP_ENDPOINT_ENV):
        otel = _otel_handler()
        if otel is not None:
            root.addHandler(otel)


def get_logger(name: str, **bound: Any) -> BoundLogger:
    _ensure_configured()
    return BoundLogger(logging.getLogger(name), bound)
