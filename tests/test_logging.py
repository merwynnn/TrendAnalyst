"""Structured-logging tests (spec §9).

The point of these is the run id: a log line without one is unattributable, and the
monitor agent cannot follow a nightly run through it.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from pydantic import SecretStr

from trend_analyst.logging import (
    ROOT_LOGGER_NAME,
    bind_run_id,
    configure_logging,
    current_run_id,
    get_logger,
    log_event,
)


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


def read_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_json_lines_carry_the_bound_run_id(stream: io.StringIO) -> None:
    logger = configure_logging(level="INFO", json_output=True, run_id="run-42", stream=stream)
    logger.info("collecting source")

    payload = read_lines(stream)[0]
    assert payload["run_id"] == "run-42"
    assert payload["level"] == "INFO"
    assert payload["logger"] == ROOT_LOGGER_NAME
    assert payload["message"] == "collecting source"
    assert str(payload["ts"]).startswith("20")


def test_run_id_can_be_rebound_per_context(stream: io.StringIO) -> None:
    logger = configure_logging(json_output=True, stream=stream)
    bind_run_id("first")
    logger.info("one")
    bind_run_id("second")
    logger.info("two")

    assert [payload["run_id"] for payload in read_lines(stream)] == ["first", "second"]
    assert current_run_id() == "second"


def test_lines_without_a_run_id_say_so(stream: io.StringIO) -> None:
    logger = configure_logging(json_output=True, stream=stream)
    bind_run_id(None)
    logger.info("outside a run")

    assert read_lines(stream)[0]["run_id"] is None


def test_log_event_carries_structured_fields(stream: io.StringIO) -> None:
    logger = configure_logging(json_output=True, run_id="run-7", stream=stream)
    log_event(logger, "source.fetched", source_id="hn_firebase", items=12, decision="ok")

    payload = read_lines(stream)[0]
    assert payload["event"] == "source.fetched"
    assert payload["source_id"] == "hn_firebase"
    assert payload["items"] == 12
    assert payload["decision"] == "ok"


def test_configure_logging_is_idempotent(stream: io.StringIO) -> None:
    """Stacked handlers would emit every line twice — a silent failure of its own."""
    logger = configure_logging(json_output=True, stream=stream)
    logger = configure_logging(json_output=True, stream=stream)
    logger.info("once")

    assert len(logger.handlers) == 1
    assert len(read_lines(stream)) == 1


def test_level_is_respected(stream: io.StringIO) -> None:
    logger = configure_logging(level="WARNING", json_output=True, stream=stream)
    logger.info("not interesting")
    logger.warning("interesting")

    payloads = read_lines(stream)
    assert len(payloads) == 1
    assert payloads[0]["level"] == "WARNING"


def test_child_loggers_share_the_configuration(stream: io.StringIO) -> None:
    configure_logging(json_output=True, run_id="run-9", stream=stream)
    child = get_logger("pipeline.l0")
    child.info("layer started")

    payload = read_lines(stream)[0]
    assert payload["logger"] == f"{ROOT_LOGGER_NAME}.pipeline.l0"
    assert payload["run_id"] == "run-9"


def test_text_formatter_is_readable_and_still_has_the_run_id(stream: io.StringIO) -> None:
    logger = configure_logging(json_output=False, run_id="run-3", stream=stream)
    log_event(logger, "layer.start", layer="L0")

    line = stream.getvalue().strip()
    assert "INFO" in line
    assert "[run-3]" in line
    assert "layer.start" in line
    assert '{"event": "layer.start", "layer": "L0"}' in line


def test_secrets_are_masked_by_the_formatter(stream: io.StringIO) -> None:
    """Keys are SecretStr, so even an accidental `extra` cannot leak one."""
    logger = configure_logging(json_output=True, run_id="run-1", stream=stream)
    log_event(logger, "gate.call", api_key=SecretStr("super-secret-value"))

    rendered = stream.getvalue()
    assert "super-secret-value" not in rendered
    assert "**********" in rendered


def test_exception_info_is_serialised(stream: io.StringIO) -> None:
    logger = configure_logging(json_output=True, run_id="run-1", stream=stream)
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("source failed")

    payload = read_lines(stream)[0]
    assert payload["level"] == "ERROR"
    assert "ValueError: boom" in str(payload["exception"])


def test_get_logger_defaults_to_the_package_logger() -> None:
    assert get_logger().name == ROOT_LOGGER_NAME
    assert isinstance(get_logger(), logging.Logger)
