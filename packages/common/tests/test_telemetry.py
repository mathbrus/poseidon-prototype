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
    log = get_logger("t2").bind(run_id="run-x", agent_id="backend-engineer-1")
    log.info("first")
    log.warning("second")

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert all(r["run_id"] == "run-x" for r in records)
    assert all(r["agent_id"] == "backend-engineer-1" for r in records)
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
