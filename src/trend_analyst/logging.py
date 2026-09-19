"""Structured logging with `run_id` on every line (spec §9).

Two rules from the spec drive this module:

* **Every log line carries the run id** — a nightly run touches dozens of sources and
  three layers, and an unattributable line is noise. The run id travels in a
  :mod:`contextvars` variable, so call sites never have to thread it through.
* **Logs are structured** — JSON by default (one object per line, ready for `jq` or the
  monitor agent), with a human-readable formatter for development.

Secrets never reach a log line: values are rendered through `str()`, and configuration
secrets are `SecretStr` (see config/settings.py), which renders as `**********`.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "ROOT_LOGGER_NAME",
    "JsonFormatter",
    "TextFormatter",
    "bind_run_id",
    "configure_logging",
    "current_run_id",
    "get_logger",
    "log_event",
]

ROOT_LOGGER_NAME = "trend_analyst"

#: The run id of whatever the current task belongs to. None outside a run.
_run_id: ContextVar[str | None] = ContextVar("trend_analyst_run_id", default=None)

#: Attributes LogRecord always carries; anything else found on a record is an
#: application field and is emitted as structured data.
_STANDARD_ATTRIBUTES = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName", "run_id",
    }
)


def bind_run_id(run_id: str | None) -> None:
    """Bind the run id that every subsequent log line in this context will carry."""
    _run_id.set(run_id)


def current_run_id() -> str | None:
    """The bound run id, or None outside a run."""
    return _run_id.get()


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRIBUTES and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with `run_id` on every record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", None) or current_run_id(),
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable lines for development, still with the run id."""

    def __init__(self) -> None:
        super().__init__("%(levelname)-8s %(name)s [%(run_id)s] %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, "run_id", None):
            record.run_id = current_run_id() or "-"
        rendered = super().format(record)
        extra = _extra_fields(record)
        if extra:
            rendered = f"{rendered} {json.dumps(extra, default=str, sort_keys=True)}"
        return rendered


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
    run_id: str | None = None,
    stream: Any = None,
) -> logging.Logger:
    """Install a single handler on the `trend_analyst` logger and return it.

    Idempotent: calling it twice replaces the handler instead of stacking duplicates
    (duplicated log lines are their own kind of silent failure).
    """
    if run_id is not None:
        bind_run_id(run_id)

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    logger.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """A child logger of the package logger (`trend_analyst.<name>`)."""
    return logging.getLogger(ROOT_LOGGER_NAME if not name else f"{ROOT_LOGGER_NAME}.{name}")


def log_event(
    logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any
) -> None:
    """Log a structured event: ``log_event(log, "source.fetched", source_id="hn_firebase")``.

    Application fields become top-level JSON keys, so the monitor agent can filter on them
    without parsing prose.
    """
    logger.log(level, event, extra={"event": event, **fields})
