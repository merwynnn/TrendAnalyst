"""Run rows and per-source cursors.

Single-shot by design — every invocation opens its own run and runs every requested
source. A crash means calling again from scratch; dedup and idempotent inserts keep
the re-run cheap. There is no resume, no pending-source tracking, no quota ledger.
The per-source cursor below is the only cross-run memory, and it is a plain read/write.

The one piece of cross-run memory kept is the per-source cursor (watermark): without
it every nightly re-fetches the full window (HN's 60 stories, Reddit's 7 days) instead
of asking "what's new since last time". It is a plain read/write on the source row —
no resume logic, no CAS.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from trend_analyst.store.models import Run, Source

__all__ = ["close_run", "open_run", "read_cursor", "write_cursor"]


def open_run(session: Session, *, trigger: str) -> uuid.UUID:
    """Open a fresh run row and return its id."""
    run = Run(status="running", trigger=trigger, layer_status={})
    session.add(run)
    session.flush()
    return run.id


def close_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    status: str,
    layer_status: dict[str, object] | None = None,
    notes: str | None = None,
) -> None:
    """Close a run with its outcome."""
    run = session.get(Run, run_id)
    if run is None:
        return
    run.status = status
    run.finished_at = datetime.now(UTC)
    if layer_status is not None:
        run.layer_status = layer_status
    run.notes = notes
    session.flush()


def read_cursor(session: Session, source_id: str) -> str | None:
    """The source's cursor, or None if it has never run."""
    return session.execute(
        select(Source.watermark).where(Source.id == source_id)
    ).scalar_one_or_none()


def write_cursor(session: Session, source_id: str, cursor: str | None) -> bool:
    """Move a source's cursor forward. A None cursor never clears a stored one,
    and an identical cursor is not rewritten."""
    if cursor is None:
        return False
    if read_cursor(session, source_id) == cursor:
        return False
    session.execute(
        update(Source)
        .where(Source.id == source_id, Source.watermark.is_distinct_from(cursor))
        .values(watermark=cursor, watermark_updated_at=datetime.now(UTC))
    )
    session.flush()
    return True
