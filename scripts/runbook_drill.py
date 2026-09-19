"""Simulate the five §9 incident runbooks against a staging copy, and record the handling.

Spec §8 asks for exactly this: *"monitor dry-run against staging logged"*. A monitor that has never
been shown a real incident is a document, not a system, so this script manufactures each incident
    in a
throwaway database and runs the real detection code against it.

**What is real, and what is a stand-in.** The detectors are the shipped modules —
    `monitor/alerts.py`
and `monitor/drift.py`, unchanged. The injection writes the same rows a bad night would write. The
*agent* is a deterministic policy table (SAFE -> act, APPROVAL -> propose, FORBIDDEN -> refuse)
    rather
than a language model, and that is deliberate: a transcript produced by a nondeterministic agent
    would
not be reproducible, and §8 wants evidence. This proves the detection, the tiering, the
    reversibility
and the cures; it does not prove that an LLM agent is competent, and the transcript says so.

Each incident runs in four steps:

1. **inject** — write the rows (or mutate the payload) that a real bad night would produce;
2. **detect** — `collect_alerts` must report the expected rule, severity and runbook;
3. **act** — the tier decision is recorded; SAFE actions are taken (against a staging copy of the
   config for anything that would touch the repo), APPROVAL actions are proposed and *not* applied;
4. **verify** — the cure is applied to staging and the alert must go quiet, because an alert whose
   fix does not silence it is a false alarm with extra steps.

Usage::

    uv run python -m scripts.runbook_drill              # staging DB, drop when done
    uv run python -m scripts.runbook_drill --keep       # leave the staging database in place
    uv run python -m scripts.runbook_drill --json       # one JSON document
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import ConfigError, Settings, load_settings
from trend_analyst.monitor.alerts import Alert, collect_alerts
from trend_analyst.monitor.drift import record_eval_run
from trend_analyst.pipeline.state import record_spend, start_run
from trend_analyst.store.db import DatabaseNotConfiguredError, create_db_engine
from trend_analyst.store.models import (
    Candidate,
    EvalCase,
    Judgement,
    Run,
    RunSourceLog,
    Source,
)

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
STAGING_DB: Final = "trend_analyst_staging"
EVIDENCE: Final = REPO_ROOT / "docs" / "evidence" / "P5-runbooks.md"

#: The tier table from AGENT.md §3, as code. The drill *is* bound by it: an APPROVAL incident must
#: end with a written proposal and an unchanged system, or the drill is lying about the boundaries.
TIERS: Final[dict[str, str]] = {
    "rate_limit_storm": "SAFE",
    "quota_burn": "APPROVAL",
    "source_failure_streak": "APPROVAL",
    "keep_rate_out_of_band": "APPROVAL",
    "keep_rate_moved": "SAFE",
    "eval_baseline_drop": "APPROVAL",
    "stale_watermark": "APPROVAL",
}


@dataclass
class Step:
    """One incident's transcript: injected, detected, decided, verified."""

    number: int
    title: str
    runbook: str
    injected: str
    evidence: list[str] = field(default_factory=list)
    detected: list[Alert] = field(default_factory=list)
    expected_rule: str = ""
    tier: str = ""
    action: str = ""
    verified: str = ""
    passed: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "runbook": self.runbook,
            "injected": self.injected,
            "detected": [alert.as_dict() for alert in self.detected],
            "expected_rule": self.expected_rule,
            "tier": self.tier,
            "action": self.action,
            "verified": self.verified,
            "passed": self.passed,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- staging lifecycle


def staging_dsn(settings: Settings, database: str = STAGING_DB) -> str:
    """The configured DSN with its database name swapped for the staging one.

    Derived rather than configured: a second DSN in `secrets.local.yaml` is one more thing the human
    can get wrong, and the drill must never be able to point at the real database by accident.
    """
    dsn = settings.db.dsn
    head, _, tail = dsn.partition("?")
    prefix, slash, name = head.rpartition("/")
    if not slash or not name:
        raise ConfigError(f"cannot derive a staging DSN from {dsn!r}: no database name in it")
    if name == database:
        raise ConfigError("the configured DSN is already the staging database; nothing to do")
    return f"{prefix}/{database}{tail}"


def _staging_db(action: str) -> str:
    """Create/drop the staging database through the privileged shell helper.

    Creating a database needs CREATEDB and the application role deliberately lacks it, so this
    goes through `scripts/staging_db.sh` as the postgres superuser. The app process holding the
    DSN therefore cannot create or destroy a database — which is the property worth keeping.
    """
    script = REPO_ROOT / "scripts" / "staging_db.sh"
    if sys.platform == "win32":
        command = [
            "wsl", "-d", "Ubuntu", "-u", "root", "--", "bash",
            str(script).replace("C:\\", "/mnt/c/").replace("\\", "/"),
            action,
        ]
    else:
        command = ["bash", str(script), action]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        raise ConfigError(
            f"`{action}` on the staging database failed ({completed.returncode}): {output}"
        )
    return output


def recreate_staging(settings: Settings) -> tuple[str, Engine]:
    """Drop and recreate the staging database, migrate it to head, and return its engine."""
    dsn = staging_dsn(settings)
    _staging_db("create")
    _migrate(dsn)
    return dsn, create_db_engine(url=dsn)


def _migrate(dsn: str) -> None:
    """Run the committed migrations — the staging schema is the real schema, not create_all()."""
    from alembic import command  # noqa: PLC0415 — alembic is a dev dependency, imported on demand
    from alembic.config import Config  # noqa: PLC0415

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn)
    command.upgrade(config, "head")


def drop_staging(settings: Settings) -> None:
    """Remove the staging database so the drill leaves no copy of the data behind."""
    _staging_db("drop")


# --------------------------------------------------------------------------- injection helpers


def drill_subjects(settings: Settings) -> tuple[str, str]:
    """Pick the sources the drill acts on: one with a recorded fixture, plus the Tier-A one.

    Chosen from the registry rather than hardcoded, because a renamed source id would otherwise turn
    the drill into a silent no-op — the failure mode this whole phase is about.
    """
    entries = _registry_sources(settings)
    with_fixture = [
        entry["id"]
        for entry in entries
        if (REPO_ROOT / "tests" / "data" / "http" / f"{entry['id']}.json").is_file()
    ]
    tier_a = [entry["id"] for entry in entries if entry["tier"] == "A"]
    if not with_fixture:
        raise ConfigError("no source has a recorded fixture; run scripts/record_fixtures.py first")
    return with_fixture[0], (tier_a[0] if tier_a else with_fixture[0])


def _registry_sources(settings: Settings) -> list[dict[str, Any]]:
    from trend_analyst.sources.registry import load_registry  # noqa: PLC0415

    registry = load_registry(REPO_ROOT / "config" / "sources.yaml")
    return [entry.model_dump(mode="json") for entry in registry.sources]


def seed_staging(session: Session, settings: Settings) -> None:
    """Copy the *shape* of the real registry into staging: the sources the alerts watch.

    Only the registry (config, not data): the drill must not copy the production raw lake into a
    second database, and a source row is all the alert rules need.
    """
    for entry in _registry_sources(settings):
        session.add(
            Source(
                id=entry["id"],
                role=entry["role"],
                tier=entry["tier"],
                layers=entry["layers"],
                schedule=entry["schedule"],
                budget_per_day=entry["budget_per_day"],
                rps=entry["rps"],
                cache_ttl_h=entry.get("cache_ttl_h", 24),
                enabled=True,
                domains=entry["domains"],
                watermark=None,
                watermark_updated_at=datetime.now(UTC),
            )
        )
    session.flush()


def _ok_baseline(session: Session, *, sources: list[str], nights: int = 3) -> None:
    """Healthy nights: sources run, nothing is throttled, nothing fails."""
    for index in range(nights):
        run = Run(
            id=uuid.uuid4(),
            started_at=datetime.now(UTC) - timedelta(days=index + 1),
            status="ok",
            trigger="nightly",
            layer_status={"L0": {"status": "ok"}},
        )
        session.add(run)
        session.flush()
        for source_id in sources:
            session.add(
                RunSourceLog(
                    run_id=run.id,
                    source_id=source_id,
                    status="ok",
                    started_at=run.started_at,
                    items_fetched=10,
                    items_new=3,
                )
            )
    session.flush()


def _judge_run(session: Session, *, decisions: list[str], nights_ago: int) -> None:
    """A judge night: one verdict per candidate, ``decisions`` in order."""
    run = Run(
        id=uuid.uuid4(),
        started_at=datetime.now(UTC) - timedelta(days=nights_ago),
        status="ok",
        trigger="nightly",
        layer_status={"L3": {"status": "ok"}},
    )
    session.add(run)
    session.flush()
    for index, decision in enumerate(decisions):
        candidate = Candidate(
            phrase=f"{decision} candidate {nights_ago}-{index}",
            category="home",
            status="kept" if decision == "keep" else "dropped",
            first_seen_at=run.started_at,
        )
        session.add(candidate)
        session.flush()
        session.add(
            Judgement(
                candidate_id=candidate.id,
                run_id=run.id,
                gate="judge",
                decision=decision,
                confidence=0.7,
                reason="drill",
                created_at=run.started_at,
            )
        )
    session.flush()


# --------------------------------------------------------------------------- the five incidents


def _alerts_of(alerts: list[Alert], rule: str) -> list[Alert]:
    return [alert for alert in alerts if alert.rule == rule]


def _incident_baseline(session: Session, settings: Settings) -> Step:
    step = Step(
        number=0,
        title="Baseline: a healthy system",
        runbook="(none)",
        injected="three healthy nights, all sources ok, no burn, no drift",
        expected_rule="",
        tier="(none)",
    )
    step.evidence.append("uv run python -m trend_analyst.monitor.alerts")
    alerts = collect_alerts(session)
    step.detected = alerts
    if not alerts:
        step.action = "nothing to do"
        step.verified = "the monitor was silent on a healthy system"
        step.passed = True
    else:
        step.notes.append(
            "a healthy system must be silent; an alert here is a false alarm that trains "
            "its reader to ignore the monitor"
        )
    return step


def _incident_rate_limit(session: Session, settings: Settings, *, source: str) -> Step:
    step = Step(
        number=1,
        title="HTTP 429 storm from a source",
        runbook="http-429-storm",
        injected=f"four nights in which {source} hit rate limits",
        expected_rule="rate_limit_storm",
    )
    for index in range(4):
        run = Run(
            id=uuid.uuid4(),
            started_at=datetime.now(UTC) - timedelta(hours=6 + index),
            status="degraded",
            trigger="nightly",
            layer_status={"L0": {"status": "degraded"}},
        )
        session.add(run)
        session.flush()
        session.add(
            RunSourceLog(
                run_id=run.id,
                source_id=source,
                status="degraded",
                started_at=run.started_at,
                items_fetched=2,
                rate_limit_hits=1,
                reason="429 from the source after 3 attempts",
            )
        )
    session.flush()

    step.evidence.append(f"uv run python -m trend_analyst.health --json  # {source} shows degraded")
    found = _alerts_of(collect_alerts(session), "rate_limit_storm")
    step.detected = found
    if not found:
        step.notes.append("expected a rate_limit_storm alert and got none")
        return step
    alert = found[0]
    step.tier = TIERS[alert.rule]
    if step.tier != "SAFE":
        step.notes.append(f"expected this runbook to be SAFE, alert says {step.tier}")

    # SAFE: the monitor may act alone, against a staging copy of the config.
    staged = _stage_config()
    entry = _patch_source_rps(staged, source_id=source, rps=0.5)
    step.action = (
        f"lowered {source}.rps to 0.5 in a staging copy of sources.yaml ({entry}); "
        "the real config is untouched until a human reviews the diff"
    )
    step.verified = _verify_config_loads(staged)
    step.passed = bool(step.verified.startswith("valid"))
    step.notes.append("reversal: `git revert` is not needed — nothing in the repo changed")
    return step


def _stage_config() -> Path:
    """Copy `config/` into a temp dir so a SAFE action can be rehearsed without touching the
        repo."""
    staged = Path(tempfile.mkdtemp(prefix="ta-drill-config-")) / "config"
    shutil.copytree(REPO_ROOT / "config", staged,
        ignore=shutil.ignore_patterns("secrets.local.yaml"))
    return staged


def _patch_source_rps(staged_config: Path, *, source_id: str, rps: float) -> str:
    """Edit one source's rps in the staged YAML. Returns a one-line description of the diff."""
    path = staged_config / "sources.yaml"
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    index = next(
        (i for i, line in enumerate(lines) if line.strip().startswith(f"id: {source_id}")),
        None,
    )
    if index is None:
        return f"source {source_id} not found in the staged config"
    for offset in range(index, min(index + 30, len(lines))):
        if lines[offset].strip().startswith("rps:"):
            before = lines[offset]
            lines[offset] = f"    rps: {rps}"
            path.write_text("\n".join(lines), encoding="utf-8")
            return f"{source_id}: `{before.strip()}` -> `rps: {rps}`"
    return f"source {source_id} has no rps field"


def _verify_config_loads(staged_config: Path) -> str:
    from trend_analyst.sources.registry import load_registry  # noqa: PLC0415

    try:
        registry = load_registry(staged_config / "sources.yaml")
    except Exception as exc:
        return f"INVALID: the staged config no longer loads: {exc}"
    return (
        f"valid: the staged config still loads ({len(registry.sources)} sources, all rules pass)"
    )


def _incident_schema_change(session: Session, settings: Settings, *, source: str) -> Step:
    """A source payload that changed shape, run through the *real* collector.

    The interesting assertion is not the alert — it is that the plugin fails loudly. A parser that
    returned zero items would look identical to a quiet week, and the system would keep reporting
        `ok`
    while collecting nothing.
    """
    step = Step(
        number=2,
        title="Source approval or schema change breaks a plugin",
        runbook="source-schema-change",
        injected=f"the recorded {source} payload with its required fields renamed",
        expected_rule="source_failure_streak",
    )
    failure = _run_collector_on_mutated_fixture(source)
    step.evidence.append(failure["command"])
    step.notes.append(f"collector result: {failure['result']}")

    if not failure["loud"]:
        step.notes.append(
            "the collector accepted a payload it should have rejected — a silent zero is worse "
            "than a failure"
        )
        return step

    # the same failure three nights running, which is what makes it a streak rather than a night
    for index in range(3):
        run = Run(
            id=uuid.uuid4(),
            started_at=datetime.now(UTC) - timedelta(days=index + 1),
            status="failed",
            trigger="nightly",
            layer_status={"L0": {"status": "failed"}},
        )
        session.add(run)
        session.flush()
        session.add(
            RunSourceLog(
                run_id=run.id,
                source_id=source,
                status="failed",
                started_at=run.started_at,
                reason=failure["result"][:200],
            )
        )
    session.flush()

    found = _alerts_of(collect_alerts(session), "source_failure_streak")
    step.detected = found
    if not found:
        step.notes.append("expected a source_failure_streak alert and got none")
        return step
    alert = found[0]
    step.tier = TIERS[alert.rule]
    if step.tier != "APPROVAL":
        step.notes.append(f"expected APPROVAL for {alert.rule}, table says {step.tier}")
        return step

    step.action = (
        "PROPOSED, not applied: disable the source for 7 days while its parser is fixed "
        "(tier APPROVAL — a schema change needs a code change, not a retry)"
    )
    step.verified = _verify_disable_cures(session, source=source)
    step.passed = step.verified.startswith("silent")
    step.notes.append("reversal: re-enable the source (`enabled: true` in sources.yaml)")
    return step


def _run_collector_on_mutated_fixture(source: str) -> dict[str, Any]:
    """Feed the real collector a payload with its content fields renamed, and compare outcomes.

    The claim being tested is the system's own anomaly rule: a source that *fetched* items and
    parsed *none* is broken, not quiet (`SourceOutcome.deduplicated`). A plugin that returned zero
    signals without noticing would look exactly like a silent week, so the drill accepts either
    refusal — an exception, or zero parsed signals where the healthy payload yielded some.
    """
    from trend_analyst.net import load_fixture  # noqa: PLC0415
    from trend_analyst.sources.base import load_plugin  # noqa: PLC0415
    from trend_analyst.sources.registry import load_registry  # noqa: PLC0415

    registry = load_registry(REPO_ROOT / "config" / "sources.yaml")
    entry = registry.by_id(source)
    fixture = load_fixture(REPO_ROOT / "tests" / "data" / "http" / f"{source}.json")
    plugin = load_plugin(entry)
    domains = tuple(entry.domains)

    healthy_count, healthy_error = _collect(f"{source} on the recording", plugin, domains, fixture)
    mutated = copy.deepcopy(fixture)
    renamed = _rename_content_fields(mutated)
    mutated_count, mutated_error = _collect(
        f"{source} on the mutated recording", plugin, domains, mutated
    )

    command = (
        f"(offline, no network) renamed {', '.join(renamed) or '(nothing)'} in "
        f"tests/data/http/{source}.json, then re-ran {entry.module}: fetch() + parse()"
    )
    if healthy_error is not None:
        return {
            "command": command,
            "result": f"the HEALTHY recording already fails ({healthy_error}) — fix the fixture",
            "loud": False,
        }
    if mutated_error is not None:
        result = (
            f"refused loudly: {mutated_count} signal(s), raised {mutated_error}"
        )
        loud = True
    elif healthy_count > 0 and mutated_count == 0:
        result = (
            f"reported the break: 0 signals parsed from a mutated payload that yields "
            f"{healthy_count} healthy ones — the orchestrator marks this degraded"
        )
        loud = True
    else:
        result = (
            f"MISMATCH: {mutated_count} signal(s) still parsed after renaming "
            f"{', '.join(renamed)} (healthy: {healthy_count})"
        )
        loud = False
    return {"command": command, "result": result, "loud": loud}


def _collect(
    label: str, plugin: Any, domains: tuple[str, ...], fixture: dict[str, Any]
) -> tuple[int, str | None]:
    """Run a plugin's fetch+parse against a fixture. Returns ``(signal count, error)``.

    Mirrors what the orchestrator does — build a context, fetch, parse — because the point is to
    exercise the *real* code path a nightly run would take, not a parser in isolation.
    """
    from trend_analyst.net import RecordedHttpClient  # noqa: PLC0415
    from trend_analyst.sources.base import (  # noqa: PLC0415
        FetchContext,
        FixedClock,
        SourceBudget,
    )

    source_id = str(getattr(plugin, "id", "?"))
    clock = FixedClock(datetime(2026, 9, 19, 12, 0, tzinfo=UTC))
    budget = SourceBudget(source_id=source_id, budget_per_day=10_000, rps=1_000.0, clock=clock)
    client = RecordedHttpClient(source_id=source_id, allowed_domains=domains, fixture=fixture)
    # The recording only contains the item pages it captured, so replay must cap the fan-out the
    # same way the orchestrator does (from the fixture's own `max_items`) or the plugin asks for a
    # page nobody recorded and the drill would report a fixture gap as a schema change.
    ctx = FetchContext(
        run_id="drill",
        source_id=source_id,
        client=client,
        budget=budget,
        clock=clock,
        max_items=int(fixture.get("max_items") or 0) or None,
    )
    try:
        batch = plugin.fetch(ctx)
        signals = plugin.parse(batch)
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {str(exc)[:160]}"
    return len(signals), None


def _rename_content_fields(fixture: dict[str, Any]) -> list[str]:
    """Rename the fields that carry meaning, wherever they appear in the payload.

    Priority-ordered: renaming ``title`` breaks a headline miner, renaming ``score`` breaks a
    ranking reader, and renaming an id breaks identity. Two of those is a schema change; renaming a
    field nothing reads would prove nothing.
    """
    priority = (
        "title",
        "score",
        "points",
        "body",
        "selftext",
        "description",
        "text",
        "name",
        "url",
        "id",
    )
    renamed: list[str] = []
    for response in fixture.get("responses", {}).values():
        payload = json.loads(response["body"])
        for field_name in _present_fields(payload, priority):
            _rename_field(payload, field_name)
            renamed.append(field_name)
        response["body"] = json.dumps(payload)
    return sorted(set(renamed))


#: How many content fields to rename for the schema-change incident. Two is enough to break a
#: parser that reads either, without turning the payload into noise nothing could parse.
_SCHEMA_CHANGE_FIELDS = 2


def _present_fields(payload: Any, priority: tuple[str, ...]) -> list[str]:
    """The first two priority fields that appear anywhere in this payload."""
    found: list[str] = []
    for field_name in priority:
        if _contains_field(payload, field_name):
            found.append(field_name)
        if len(found) >= _SCHEMA_CHANGE_FIELDS:
            break
    return found


def _contains_field(payload: Any, field_name: str) -> bool:
    if isinstance(payload, dict):
        return field_name in payload or any(
            _contains_field(value, field_name) for value in payload.values()
        )
    if isinstance(payload, list):
        return any(_contains_field(value, field_name) for value in payload)
    return False


def _rename_field(payload: Any, field_name: str) -> None:
    """Rename a field wherever it appears, so the plugin's expectation is genuinely violated."""
    if isinstance(payload, dict):
        if field_name in payload:
            payload[f"{field_name}__renamed"] = payload.pop(field_name)
        for value in payload.values():
            _rename_field(value, field_name)
    elif isinstance(payload, list):
        for value in payload:
            _rename_field(value, field_name)


def _verify_disable_cures(session: Session, *, source: str) -> str:
    """Apply the proposed change to staging and confirm the alert stops firing."""
    row = session.get(Source, source)
    if row is None:
        return "source missing in staging"
    row.enabled = False
    session.flush()
    remaining = _alerts_of(collect_alerts(session), "source_failure_streak")
    return "silent: disabling the source clears the alert" if not remaining else (
        f"STILL ALERTING: {len(remaining)} alert(s) remain after the proposed change"
    )


def _incident_quota_burn(session: Session, settings: Settings, *, source: str) -> Step:
    step = Step(
        number=3,
        title="Quota burn above 80 percent",
        runbook="quota-burn",
        injected=f"{source} spent 85% of its daily budget (then 100%)",
        expected_rule="quota_burn",
    )
    handle = start_run(session, trigger="manual", resume=False)
    budget = int(session.get(Source, source).budget_per_day)  # type: ignore[union-attr]
    first = int(budget * 0.85)
    record_spend(
        session, source_id=source, amount=first, operation="drill:1", run_id=handle.run_id
    )
    step.evidence.append(
        f"ledger: record_spend({source}, {first}) -> 85% of {budget} for the day"
    )

    found = _alerts_of(collect_alerts(session), "quota_burn")
    step.detected = found
    if not found:
        step.notes.append("expected a quota_burn alert and got none")
        return step
    alert = found[0]
    step.tier = TIERS[alert.rule]
    step.action = (
        "PROPOSED, not applied: reduce the source's budget or let it skip until tomorrow "
        "(tier APPROVAL — a budget change spends real quota or narrows coverage)"
    )

    # The leash, demonstrated: a resumed run re-issuing the same spend must lose the race.
    step.notes.append(_verify_ledger_cas(session, source=source, run_id=handle.run_id))

    # the rest of the budget, then the cure: past 100% the alarm is critical, and the fix is silence
    record_spend(
        session,
        source_id=source,
        amount=budget - first,
        operation="drill:2",
        run_id=handle.run_id,
    )
    second = _alerts_of(collect_alerts(session), "quota_burn")
    severity = second[0].severity if second else "(none)"
    step.notes.append(f"at 100% of budget the alert severity is {severity}")

    # The cure is the calendar, not a deletion: the budget is per day, so tomorrow's window is
    # empty. (Deleting ledger rows to silence this alert would be a FORBIDDEN change — the ledger is
    # append-only on purpose, which is exactly why the drill demonstrates the rollover instead.)
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    remaining = _alerts_of(collect_alerts(session, now=tomorrow), "quota_burn")
    step.verified = (
        "silent after the day rolls over (the next day's window is empty; the ledger row stays)"
        if not remaining
        else f"STILL ALERTING: {len(remaining)} quota alert(s) remain"
    )
    step.passed = bool(step.tier == "APPROVAL" and step.verified.startswith("silent"))
    step.notes.append(
        "reversal: budgets are config (git revert the sources.yaml commit); nothing was deleted, "
        "so there is nothing to restore"
    )
    return step


def _verify_ledger_cas(session: Session, *, source: str, run_id: uuid.UUID) -> str:
    """Re-issue a spend that already happened: the ledger must refuse to charge it twice."""
    try:
        charged = record_spend(
            session, source_id=source, amount=1, operation="drill:1", run_id=run_id
        )
    except Exception as exc:  # the ledger raising would be an equally clear refusal
        return f"the ledger refused a duplicate spend ({type(exc).__name__})"
    if charged:
        return "MISMATCH: the ledger accepted a duplicate spend"
    return "the ledger refused a duplicate spend (CAS held: a resumed run cannot pay twice)"


def _incident_keep_rate(session: Session, settings: Settings) -> Step:
    step = Step(
        number=4,
        title="Judge keep-rate drift outside the band",
        runbook="keep-rate-drift",
        injected="two judge nights that kept everything (8 of 8, then 8 of 8)",
        expected_rule="keep_rate_out_of_band",
    )
    _judge_run(session, decisions=["keep"] * 8, nights_ago=2)
    _judge_run(session, decisions=["keep"] * 8, nights_ago=1)

    found = _alerts_of(collect_alerts(session), "keep_rate_out_of_band")
    step.detected = found
    if not found:
        step.notes.append("expected a keep_rate_out_of_band alert and got none")
        return step
    alert = found[0]
    step.tier = TIERS[alert.rule]
    step.action = (
        "PROPOSED, not applied: review the last judgements' reasons and the prompt "
        "(tier APPROVAL — a judge that keeps everything makes every downstream cost unjustified)"
    )

    # verify by curing: a judge that discriminates again puts the rate back inside the band
    _judge_run(session, decisions=["keep", "drop"] * 8, nights_ago=0)
    remaining = _alerts_of(collect_alerts(session), "keep_rate_out_of_band")
    step.verified = (
        "silent after a night of discriminating verdicts (keep-rate back inside 5%-80%)"
        if not remaining
        else f"STILL ALERTING: {len(remaining)} alert(s) remain"
    )
    step.passed = bool(step.tier == "APPROVAL" and step.verified.startswith("silent"))
    step.notes.append("reversal: `git revert <sha>` of the prompt or weights change")
    return step


def _incident_eval_baseline(session: Session, settings: Settings) -> Step:
    step = Step(
        number=5,
        title="Eval baseline drop",
        runbook="eval-baseline-drop",
        injected="a rubric scoring 3.0 below its stored baseline (after a 2.0 drop passed)",
        expected_rule="eval_baseline_drop",
    )
    cases = session.query(EvalCase).all()
    if not cases:
        step.notes.append("no eval cases in staging — seeding skipped")
        return step
    case_id = cases[0].id
    record_eval_run(session, scores={case_id: 9.0})
    step.evidence.append(f"record_eval_run({case_id}: 9.0) -> baseline recorded, no alert")
    tolerated = record_eval_run(session, scores={case_id: 7.0})
    step.notes.append(
        f"a 2.0 drop is tolerated as §8 requires ({len(tolerated)} alert(s))"
        if not tolerated
        else "MISMATCH: a 2.0 drop alerted"
    )
    record_eval_run(session, scores={case_id: 6.0})

    found = [alert for alert in collect_alerts(session) if alert.rule == "eval_baseline_drop"]
    step.detected = found
    if not found:
        step.notes.append("expected an eval_baseline_drop alert and got none")
        return step
    alert = found[0]
    step.tier = TIERS[alert.rule]
    step.action = (
        "PROPOSED, not applied: find the change since the baseline run before re-baselining "
        "(tier APPROVAL — re-baselining hides the regression)"
    )
    row = session.get(EvalCase, case_id)
    assert row is not None
    row.last_score = float(row.baseline_score or 0.0)
    session.flush()
    remaining = [a for a in collect_alerts(session) if a.rule == "eval_baseline_drop"]
    step.verified = (
        "silent once the rubric is back at baseline (the cure is restoring the score, not the bar)"
        if not remaining
        else f"STILL ALERTING: {len(remaining)} alert(s) remain"
    )
    step.passed = bool(step.tier == "APPROVAL" and step.verified.startswith("silent"))
    step.notes.append("reversal: `git revert <sha>` of the change since the baseline run")
    return step


def _incident_watermark(session: Session, settings: Settings, *, source: str) -> Step:
    """A cursor that stopped moving: no new signals, every run still reporting ok."""
    step = Step(
        number=6,
        title="Stale watermark (a source that reports ok and collects nothing)",
        runbook="source-schema-change",
        injected=f"{source} last moved its watermark 10 days ago",
        expected_rule="stale_watermark",
    )
    row = session.get(Source, source)
    if row is None:
        step.notes.append("source missing in staging")
        return step
    # A distinct night: the schema-change incident permanently disabled this source (that was its
    # cure), and a later incident is a different night's failure, not a continuation of that one.
    row.enabled = True
    row.watermark_updated_at = datetime.now(UTC) - timedelta(days=10)
    session.flush()

    found = _alerts_of(collect_alerts(session), "stale_watermark")
    step.detected = found
    if not found:
        step.notes.append("expected a stale_watermark alert and got none")
        return step
    alert = found[0]
    step.tier = TIERS[alert.rule]
    step.action = (
        "PROPOSED, not applied: check the source's recent payloads for a schema change "
        "(tier APPROVAL — the plugin needs the fix, not a retry)"
    )
    row = session.get(Source, source)
    assert row is not None
    row.watermark_updated_at = datetime.now(UTC)
    row.watermark = "advanced-by-the-drill"
    session.flush()
    remaining = _alerts_of(collect_alerts(session), "stale_watermark")
    step.verified = (
        "silent once the watermark advances again"
        if not remaining
        else f"STILL ALERTING: {len(remaining)} alert(s) remain"
    )
    step.passed = bool(step.tier == "APPROVAL" and step.verified.startswith("silent"))
    step.notes.append("reversal: n/a — the watermark is data, and advancing it is the cure")
    return step


# --------------------------------------------------------------------------- report


def _render_table(steps: list[Step], *, staging: str, dropped: bool) -> list[str]:
    """The header and the one-line-per-incident summary table."""
    lines = [
        "# P5 — incident runbooks, simulated against staging",
        "",
        "Spec §8: *\"monitor dry-run against staging logged\"*. Generated by",
        "`uv run python -m scripts.runbook_drill` — re-run it to reproduce this file.",
        "",
        "**What is real:** the detectors (`monitor/alerts.py`, `monitor/drift.py`), the ledger "
        "and its",
        "compare-and-swap, the source plugins, the config loader and the migrations. **What is a",
        "stand-in:** the agent's decision layer — a deterministic tier table (SAFE acts, "
        "APPROVAL",
        "proposes, FORBIDDEN refuses) rather than a language model, so this transcript is "
        "reproducible.",
        "It proves detection, tiering, reversibility and the cures; it does not prove that an "
        "LLM agent",
        "is competent, and the escalation templates below are what such an agent would have to "
        "send.",
        "",
        f"Staging database: `{redact_dsn(staging)}` "
        f"({'dropped after the drill' if dropped else 'kept'}).",
        f"Incidents: {len(steps)}. All passed: {all(step.passed for step in steps)}.",
        "",
        "| # | Incident | Rule | Tier | Detected | Verified |",
        "|---|----------|------|------|----------|----------|",
    ]
    for step in steps:
        lines.append(
            f"| {step.number} | {step.title} | `{step.expected_rule or '-'}` | "
            f"{step.tier or '-'} | {'PASS' if step.passed else 'FAIL'} | "
            f"{step.verified or '-'} |"
        )
    lines.append("")
    return lines


def _render_alerts(alerts: list[Alert]) -> list[str]:
    """Each alert with the escalation template's fields: symptom, tier, action, reversal, radius."""
    if not alerts:
        return ["- nothing (see the notes)"]
    lines: list[str] = []
    for alert in alerts:
        lines.append(
            f"- `[{alert.severity.upper()}] {alert.rule}` -> runbook `{alert.runbook}`: "
            f"{alert.symptom}"
        )
        lines.append(f"  - action ({alert.tier}): {alert.action}")
        lines.append(f"  - reversible: {alert.reversible_with}")
        lines.append(f"  - blast radius if ignored: {alert.blast_radius}")
    return lines


def _render_step(step: Step) -> list[str]:
    """One incident's section: injected, detected, decision, verdict (the template's fields)."""
    lines: list[str] = [
        f"## {step.number}. {step.title}",
        "",
        f"*Runbook:* `{step.runbook}` · *tier:* **{step.tier or '(none)'}**",
        "",
        f"**Injected.** {step.injected}",
    ]
    if step.evidence:
        lines += ["", "```bash", *step.evidence, "```"]
    lines += ["", "**Detected.**", "", *_render_alerts(step.detected)]
    lines += ["", f"**Decision.** {step.action}", ""]
    lines.append(f"**Verified.** {step.verified or '(not verified)'}")
    if step.notes:
        lines += ["", "**Notes.**", ""]
        lines += [f"- {note}" for note in step.notes]
    lines += ["", f"**Outcome: {'PASS' if step.passed else 'FAIL'}**", ""]
    return lines


def redact_dsn(dsn: str) -> str:
    """Print a DSN without its password: evidence files are committed, credentials are not."""
    if "@" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    credentials, _, location = rest.partition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{location}"


def render(steps: list[Step], *, staging: str, dropped: bool) -> str:
    lines = _render_table(steps, staging=redact_dsn(staging), dropped=dropped)
    for step in steps:
        lines.extend(_render_step(step))
    lines.append("## Escalation template (what the agent sends a human)")
    lines.append("")
    lines.append("```text")
    lines.append("SYMPTOM:      <one line, from the alert's symptom field>")
    lines.append("EVIDENCE:     uv run python -m trend_analyst.monitor.alerts --json")
    lines.append("              <the alert's evidence lines, run, with their output>")
    lines.append("ATTEMPTED:    <SAFE actions taken, with the command that verified each>")
    lines.append("PROPOSED:     <the APPROVAL-class change, as a diff or a command>")
    lines.append("BLAST RADIUS: <what breaks if nobody acts, including 'nothing' when true>")
    lines.append("REVERSAL:     <one command>")
    lines.append("```")
    lines.append("")
    lines.append("## What this drill does not cover")
    lines.append("")
    lines.append(
        "- **A human acting.** APPROVAL incidents end with a proposal here; a real night waits."
    )
    lines.append(
        "- **The agent's language.** A deterministic tier table read the alerts. §8's bar is a"
    )
    lines.append(
        "  logged dry-run, and this is one; the agent's drafting is not what is being verified."
    )
    lines.append(
        "- **Live 429s.** The storm is injected as log rows, because reproducing a real throttle"
    )
    lines.append(
        "  requires a source that throttles on demand — a fixture records responses, not refusals."
    )
    lines.append("")
    return "\n".join(lines)


def _run_incidents(sessions: sessionmaker[Session], settings: Settings) -> list[Step]:
    """Run every incident in order, in one staging session, and commit once at the end."""
    with sessions() as session:
        seed_staging(session, settings)
        _ok_baseline(session, sources=[entry["id"] for entry in _registry_sources(settings)][:3])
        _seed_eval_cases(session, settings)
        subject, tier_a_subject = drill_subjects(settings)
        steps = [
            _incident_baseline(session, settings),
            _incident_rate_limit(session, settings, source=subject),
            _incident_schema_change(session, settings, source=subject),
            _incident_quota_burn(session, settings, source=tier_a_subject),
            _incident_keep_rate(session, settings),
            _incident_eval_baseline(session, settings),
            _incident_watermark(session, settings, source=subject),
        ]
        session.commit()
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.runbook_drill",
        description="Simulate the five §9 runbooks against a staging copy and record the outcome.",
    )
    parser.add_argument("--keep", action="store_true", help="leave the staging database in place")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--evidence", default=str(EVIDENCE))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and exit without creating or dropping any database",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    if not settings.db.configured:
        print("database not configured: set db.url (see USER_SETUP.md)", file=sys.stderr)
        return 1

    plan = list(_PLANNED_INCIDENTS)
    if args.dry_run:
        print(json.dumps({"plan": plan, "staging_database": STAGING_DB}, indent=2))
        return 0

    try:
        dsn, engine = recreate_staging(settings)
    except (ConfigError, DatabaseNotConfiguredError) as exc:
        print(f"cannot create the staging database: {exc}", file=sys.stderr)
        return 1

    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        steps = _run_incidents(sessions, settings)
    finally:
        engine.dispose()

    if not args.keep:
        drop_staging(settings)

    passed = all(step.passed for step in steps)
    if args.json:
        print(
            json.dumps(
                {
                    "staging": dsn,
                    "passed": passed,
                    "steps": [step.as_dict() for step in steps],
                },
                indent=2,
                default=str,
            )
        )
    else:
        report = render(steps, staging=dsn, dropped=not args.keep)
        Path(args.evidence).parent.mkdir(parents=True, exist_ok=True)
        Path(args.evidence).write_text(report, encoding="utf-8", newline="\n")
        for step in steps:
            print(f"  {step.number}. {step.title}: {'PASS' if step.passed else 'FAIL'}")
            if not step.passed:
                for note in step.notes:
                    print(f"       {note}")
        print(f"\nRUNBOOK DRILL: {'PASS' if passed else 'FAIL'} ({len(steps)} incidents)")
        print(f"transcript: {args.evidence}")
    return 0 if passed else 1


def _seed_eval_cases(session: Session, settings: Settings) -> None:
    """Seed a couple of eval cases so the baseline-drop runbook has something to watch."""
    for case_id in ("drill-case-1", "drill-case-2"):
        session.add(
            EvalCase(
                id=case_id,
                category="home",
                input_signals=[],
                expected_keep=True,
                expected_fad_label="trend",
                score_min=40.0,
                score_max=100.0,
                notes="seeded by scripts/runbook_drill.py (not a real golden case)",
            )
        )
    session.flush()


#: The incidents, in the order the drill runs them. Named so `--dry-run` can print the plan without
#: importing half the script.
_PLANNED_INCIDENTS: Final[tuple[str, ...]] = (
    "Baseline: a healthy system",
    "HTTP 429 storm from a source",
    "Source approval or schema change breaks a plugin",
    "Quota burn above 80 percent",
    "Judge keep-rate drift outside the band",
    "Eval baseline drop",
    "Stale watermark (a source that reports ok and collects nothing)",
)


if __name__ == "__main__":
    raise SystemExit(main())
