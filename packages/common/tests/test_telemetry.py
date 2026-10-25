import io
import json
import logging
from collections.abc import Callable

import pytest

from common.telemetry import BoundLogger, JsonFormatter, get_logger


def capture(logger_name: str):
    """Attach a StringIO handler with the JSON formatter to a logger."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    underlying = logging.getLogger(logger_name)
    underlying.addHandler(handler)
    underlying.setLevel(logging.INFO)
    return stream


def test_single_line_json_with_standard_fields():
    stream = capture("t1")
    get_logger("t1").info("hello world")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["msg"] == "hello world"
    assert record["level"] == "info"
    assert record["logger"] == "t1"
    assert "ts" in record


def test_bound_context_appears_on_every_line():
    stream = capture("t2")
    log = get_logger("t2").bind(run_id="run-x", agent_id="pm-1")
    log.info("first")
    log.warning("second")

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert all(r["run_id"] == "run-x" for r in records)
    assert all(r["agent_id"] == "pm-1" for r in records)
    assert records[1]["level"] == "warning"


def test_get_logger_kwargs_and_per_call_extra():
    stream = capture("t3")
    log = get_logger("t3", run_id="run-y")
    log.info("with extra", extra={"tool": "sandbox.exec"})

    record = json.loads(stream.getvalue())
    assert record["run_id"] == "run-y"
    assert record["tool"] == "sandbox.exec"


def test_stdout_handler_configured_once():
    get_logger("t4")
    get_logger("t5")
    root = logging.getLogger()
    json_handlers = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
    assert len(json_handlers) == 1


def exported(logger_name: str, emit: Callable[[BoundLogger], None]) -> list:
    """The OTel log records `emit` logs on `logger_name`, through the OTLP
    handler, onto an in-memory exporter, in the order they were logged."""
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    from common import telemetry

    exporter = InMemoryLogRecordExporter()
    handler = telemetry._otel_handler(exporter=exporter)
    assert handler is not None

    underlying = logging.getLogger(logger_name)
    underlying.addHandler(handler)
    underlying.setLevel(logging.DEBUG)
    try:
        emit(get_logger(logger_name))
        assert handler.provider.force_flush()
    finally:
        underlying.removeHandler(handler)
    return [finished.log_record for finished in exporter.get_finished_logs()]


def test_otel_handler_exports_bound_fields_as_attributes():
    """The OTel half of the seam: bound context rides as flat, primitive
    log-record attributes (never the `bound` dict itself), the body is the
    message, and the stdout JSON contract is untouched by the extra
    handler's presence."""

    def emit(log: BoundLogger) -> None:
        log = log.bind(run_id="run-z", agent_id="pm-1")
        log.info("exported", extra={"iterations": 3, "handle": object()})

    (record,) = exported("t-otel", emit)
    assert record.body == "exported"
    attributes = dict(record.attributes)
    assert attributes["run_id"] == "run-z"
    assert attributes["agent_id"] == "pm-1"
    assert attributes["iterations"] == 3
    # Non-primitive bound values are stringified, and the dict container
    # itself never leaks through as an attribute.
    assert isinstance(attributes["handle"], str)
    assert "bound" not in attributes


@pytest.mark.parametrize(
    ("level", "number", "text"),
    [
        (logging.DEBUG, 5, "DEBUG"),
        (logging.INFO, 9, "INFO"),
        (logging.WARNING, 13, "WARN"),
        (logging.ERROR, 17, "ERROR"),
        (logging.CRITICAL, 21, "FATAL"),
    ],
)
def test_otel_severity_follows_the_level(level, number, text):
    """Each of Python's levels lands on OTel's severity of the same name,
    spelled as OTel spells it: WARNING is WARN, CRITICAL is FATAL."""
    (record,) = exported(f"t-severity-{level}", lambda log: log.log(level, "leveled"))
    assert record.severity_number.value == number
    assert record.severity_text == text


def test_otel_exception_rides_as_attributes():
    """A record logged with its exception carries the exception's type,
    message and stack, under OTel's semantic-convention names."""

    def emit(log: BoundLogger) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")

    (record,) = exported("t-exception", emit)
    attributes = dict(record.attributes)
    assert attributes["exception.type"] == "ValueError"
    assert attributes["exception.message"] == "boom"
    assert 'raise ValueError("boom")' in attributes["exception.stacktrace"]


def test_otel_record_carries_the_active_span():
    """A record logged inside a span names that span's trace and span; one
    logged outside any span names none."""
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    active = []

    def emit(log: BoundLogger) -> None:
        log.info("outside")
        with tracer.start_as_current_span("work") as span:
            active.append(span.get_span_context())
            log.info("inside")

    outside, inside = exported("t-trace", emit)
    (span,) = active
    assert span.trace_id and span.span_id
    assert (inside.trace_id, inside.span_id) == (span.trace_id, span.span_id)
    assert (outside.trace_id, outside.span_id) == (0, 0)


def test_otel_service_name_rides_the_resource(monkeypatch):
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    from common import telemetry

    monkeypatch.setenv("OTEL_SERVICE_NAME", "mcp-gateway")
    handler = telemetry._otel_handler(exporter=InMemoryLogRecordExporter())
    assert handler is not None
    resource = handler.provider.resource
    assert resource.attributes["service.name"] == "mcp-gateway"
