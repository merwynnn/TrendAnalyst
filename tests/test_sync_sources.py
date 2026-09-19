"""Registry→database sync tests (spec §7, §5.1).

The two properties that matter are asserted directly:

* **idempotency** — running it twice changes nothing the second time;
* **watermark preservation** — the sync cannot make a source re-fetch or skip work,
  because it never writes the watermark columns at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from scripts.sync_sources import REGISTRY_OWNED_COLUMNS, sync_sources
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from trend_analyst.sources.registry import default_registry_path, load_registry
from trend_analyst.store.models import Source

pytestmark = pytest.mark.db

ENTRY = """
    role: {role}
    module: trend_analyst.sources.tier_s.hn
    tier: S
    layers: [L0]
    schedule: nightly
    budget_per_day: {budget}
    rps: 1
    enabled: true
    domains: [example.com]
"""


def write_registry(tmp_path: Path, entries: dict[str, dict[str, str]]) -> Path:
    lines = ["version: 1", "sources:"]
    for source_id, fields in entries.items():
        lines.append(f"  {source_id}:")
        lines.append(ENTRY.format(**fields).rstrip())
    path = tmp_path / "sources.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def set_watermark(session: Session, source_id: str, cursor: str) -> None:
    session.execute(
        update(Source)
        .where(Source.id == source_id)
        .values(watermark=cursor, watermark_updated_at=datetime.now(UTC))
    )
    session.flush()


def stored_ids(session: Session) -> list[str]:
    return sorted(session.execute(select(Source.id)).scalars())


# ---------------------------------------------------------------------------
# First sync: insert everything
# ---------------------------------------------------------------------------
def test_first_sync_inserts_the_whole_catalog(db_session: Session, repo_root: Path) -> None:
    registry = load_registry(default_registry_path(repo_root / "config"))
    report = sync_sources(db_session, registry)

    assert len(report.inserted) == len(registry.sources) == 22
    assert report.updated == ()
    assert report.unchanged == ()
    assert stored_ids(db_session) == sorted(entry.id for entry in registry.sources)

    row = db_session.get(Source, "hn_firebase")
    assert row is not None
    assert row.tier == "S"
    assert row.layers == ["L0"]
    assert row.budget_per_day == 2000
    assert row.enabled is True
    assert row.watermark is None

    tier_a = db_session.get(Source, "ebay_browse")
    assert tier_a is not None
    assert tier_a.enabled is False, "Tier A ships disabled until its credential exists"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_second_sync_is_a_no_op(db_session: Session, repo_root: Path) -> None:
    registry = load_registry(default_registry_path(repo_root / "config"))
    sync_sources(db_session, registry)
    #: A guard for future editors: this list is the whole contract of the sync.
    before = {
        row.id: (
            row.role,
            row.tier,
            tuple(row.layers),
            row.budget_per_day,
            row.enabled,
            row.updated_at,
        )
        for row in db_session.execute(select(Source)).scalars()
    }

    report = sync_sources(db_session, registry)

    after = {
        row.id: (
            row.role,
            row.tier,
            tuple(row.layers),
            row.budget_per_day,
            row.enabled,
            row.updated_at,
        )
        for row in db_session.execute(select(Source)).scalars()
    }
    assert report.inserted == ()
    assert report.updated == ()
    assert report.disabled_missing == ()
    assert len(report.unchanged) == 22
    assert after == before, "a no-op sync must not even move updated_at"
    assert len(stored_ids(db_session)) == 22, "no duplicate rows"


def test_sync_is_idempotent_across_repeated_runs(db_session: Session, tmp_path: Path) -> None:
    registry = load_registry(write_registry(tmp_path, {"alpha": {"role": "a", "budget": "10"}}))
    for _ in range(3):
        sync_sources(db_session, registry)

    rows = db_session.execute(select(Source)).scalars().all()
    assert [row.id for row in rows] == ["alpha"]


# ---------------------------------------------------------------------------
# The watermark is runtime state, not configuration
# ---------------------------------------------------------------------------
def test_sync_never_touches_the_watermark(db_session: Session, tmp_path: Path) -> None:
    registry = load_registry(write_registry(tmp_path, {"alpha": {"role": "a", "budget": "10"}}))
    sync_sources(db_session, registry)
    set_watermark(db_session, "alpha", "cursor-42")

    # Now change something the registry owns, so the sync definitely writes the row.
    changed = load_registry(
        write_registry(tmp_path, {"alpha": {"role": "renamed", "budget": "99"}})
    )
    report = sync_sources(db_session, changed)

    assert report.updated == ("alpha",)
    row = db_session.get(Source, "alpha")
    assert row is not None
    assert row.role == "renamed"
    assert row.budget_per_day == 99
    assert row.watermark == "cursor-42", "the watermark must survive a sync byte-identically"
    assert row.watermark_updated_at is not None


def test_watermark_columns_are_not_owned_by_the_registry() -> None:
    """A guard for future editors: this list is the whole contract of the sync."""
    assert "watermark" not in REGISTRY_OWNED_COLUMNS
    assert "watermark_updated_at" not in REGISTRY_OWNED_COLUMNS


def test_sync_reports_exactly_which_fields_changed(db_session: Session, tmp_path: Path) -> None:
    registry = load_registry(write_registry(tmp_path, {"alpha": {"role": "a", "budget": "10"}}))
    sync_sources(db_session, registry)

    changed = load_registry(write_registry(tmp_path, {"alpha": {"role": "a", "budget": "25"}}))
    report = sync_sources(db_session, changed)

    assert report.changes == {"alpha": {"budget_per_day": (10, 25)}}


# ---------------------------------------------------------------------------
# Removal is a disable, never a delete
# ---------------------------------------------------------------------------
def test_source_missing_from_the_registry_is_disabled_not_deleted(
    db_session: Session, tmp_path: Path
) -> None:
    two = load_registry(
        write_registry(
            tmp_path,
            {"alpha": {"role": "a", "budget": "10"}, "beta": {"role": "b", "budget": "20"}},
        )
    )
    sync_sources(db_session, two)
    set_watermark(db_session, "beta", "cursor-b")

    one = load_registry(write_registry(tmp_path, {"alpha": {"role": "a", "budget": "10"}}))
    report = sync_sources(db_session, one)

    assert report.disabled_missing == ("beta",)
    beta = db_session.get(Source, "beta")
    assert beta is not None, "the row survives: history references this id"
    assert beta.enabled is False
    assert beta.watermark == "cursor-b", "disabling is not a reason to lose the cursor"


def test_an_already_disabled_missing_source_is_left_alone(
    db_session: Session, tmp_path: Path
) -> None:
    two = load_registry(
        write_registry(
            tmp_path,
            {"alpha": {"role": "a", "budget": "10"}, "beta": {"role": "b", "budget": "20"}},
        )
    )
    sync_sources(db_session, two)
    one = load_registry(write_registry(tmp_path, {"alpha": {"role": "a", "budget": "10"}}))
    sync_sources(db_session, one)

    report = sync_sources(db_session, one)
    assert report.disabled_missing == (), "already disabled: nothing left to do"
    assert report.already_disabled == ("beta",), "orphans stay visible in the report"
    assert report.unchanged == ("alpha",)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def test_dry_run_reports_the_work_without_writing(db_session: Session, tmp_path: Path) -> None:
    registry = load_registry(write_registry(tmp_path, {"alpha": {"role": "a", "budget": "10"}}))

    report = sync_sources(db_session, registry, dry_run=True)

    assert report.dry_run is True
    assert report.inserted == ("alpha",)
    assert stored_ids(db_session) == [], "a dry run must write nothing at all"

    applied = sync_sources(db_session, registry)
    assert applied.inserted == ("alpha",)
    assert stored_ids(db_session) == ["alpha"]


def test_dry_run_leaves_existing_rows_untouched(db_session: Session, tmp_path: Path) -> None:
    registry = load_registry(write_registry(tmp_path, {"alpha": {"role": "a", "budget": "10"}}))
    sync_sources(db_session, registry)

    changed = load_registry(write_registry(tmp_path, {"alpha": {"role": "z", "budget": "77"}}))
    report = sync_sources(db_session, changed, dry_run=True)

    assert report.updated == ("alpha",)
    row = db_session.get(Source, "alpha")
    assert row is not None
    assert (row.role, row.budget_per_day) == ("a", 10)


# ---------------------------------------------------------------------------
# The report renders
# ---------------------------------------------------------------------------
def test_report_renders_for_a_human(db_session: Session, tmp_path: Path) -> None:
    registry = load_registry(write_registry(tmp_path, {"alpha": {"role": "a", "budget": "10"}}))
    report = sync_sources(db_session, registry)

    text = report.render()
    assert "registry sync (applied)" in text
    assert "inserted:         1" in text
    assert report.summary()["inserted"] == 1
    assert report.summary()["ids"]["inserted"] == ["alpha"]
