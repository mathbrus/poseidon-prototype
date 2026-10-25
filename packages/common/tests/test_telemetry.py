import io
import json
import logging

from common.telemetry import JsonFormatter, get_logger


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


def test_otel_handler_exports_bound_fields_as_attributes():
    """The OTel half of the seam: bound context rides as flat, primitive
    log-record attributes (never the `bound` dict itself), the body is the
    message, and the stdout JSON contract is untouched by the extra
    handler's presence."""
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    from common import telemetry

    exporter = InMemoryLogRecordExporter()
    handler = telemetry._otel_handler(exporter=exporter)
    assert handler is not None

    underlying = logging.getLogger("t-otel")
    underlying.addHandler(handler)
    underlying.setLevel(logging.INFO)
    try:
        log = get_logger("t-otel").bind(run_id="run-z", agent_id="pm-1")
        log.info("exported", extra={"iterations": 3, "handle": object()})
        assert handler.provider.force_flush()
    finally:
        underlying.removeHandler(handler)

    finished = exporter.get_finished_logs()
    assert len(finished) == 1
    record = finished[0].log_record
    assert record.body == "exported"
    attributes = dict(record.attributes)
    assert attributes["run_id"] == "run-z"
    assert attributes["agent_id"] == "pm-1"
    assert attributes["iterations"] == 3
    # Non-primitive bound values are stringified, and the dict container
    # itself never leaks through as an attribute.
    assert isinstance(attributes["handle"], str)
    assert "bound" not in attributes


def test_otel_service_name_rides_the_resource(monkeypatch):
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    from common import telemetry

    monkeypatch.setenv("OTEL_SERVICE_NAME", "mcp-gateway")
    handler = telemetry._otel_handler(exporter=InMemoryLogRecordExporter())
    assert handler is not None
    resource = handler.provider.resource
    assert resource.attributes["service.name"] == "mcp-gateway"
