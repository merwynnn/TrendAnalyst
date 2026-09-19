"""Alerts: the monitor agent's inbox, and the rules that decide what goes in it (spec §9).

The monitor agent is a small, periodic, untrusted-by-design worker: it reads health, classifies,
matches a runbook, acts inside its tier, verifies and logs. This module is the part that decides
**what is worth waking up for**, and it is deliberately conservative — an alert that fires on a
healthy system trains its reader to ignore alerts.

Five rules, matching the five runbooks AGENT.md documents:

* ``rate_limit_storm`` — a source hits 429s repeatedly: continuing to hammer an integration is how
  it gets blocked.
* ``quota_burn`` — a source is past 80% of its daily budget: the budget exists to stop exactly this.
* ``source_failure_streak`` — a source failed N runs in a row: one failure is noise, a streak is a
  broken endpoint, a revoked key or a schema change.
* ``keep_rate_drift`` — the judge's keep-rate left its band: a gate that stopped discriminating
    makes
  every downstream cost unjustified.
* ``eval_baseline_drop`` — a rubric fell more than 2 points, which is §8's only guard on quality
    over
  time.

Every alert carries the fields the escalation template needs: symptom, evidence (the exact commands
and what they printed), the tier of the suggested action, and how to reverse it. Nothing here
    *acts*:
the agent acts, inside the tiers, and a module that silently changed a threshold would be exactly
    the
"unreviewable change" the boundary system exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.monitor.drift import (
    DriftFinding,
    eval_baseline_drop,
    keep_rate_drift,
    watermark_drift,
)
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import QuotaLedger, RunSourceLog, Source

__all__ = [
    "DEFAULT_QUOTA_BURN_THRESHOLD",
    "DEFAULT_RATE_LIMIT_STORM",
    "Alert",
    "collect_alerts",
    "load_outbox",
    "render_alerts",
    "write_outbox",
]

#: Fraction of a source's daily budget at which the monitor should be told (spec §9's runbook is
#: "quota burn above 80 percent").
DEFAULT_QUOTA_BURN_THRESHOLD: Final = 0.8

#: How many rate-limit hits inside the window count as a storm rather than an unlucky minute.
DEFAULT_RATE_LIMIT_STORM: Final = 3

#: How many consecutive failed runs make a source "broken" rather than "having a bad night".
DEFAULT_FAILURE_STREAK: Final = 3

#: Where the monitor's inbox lives. A file, not a table: it is a *human* queue, it must survive a
#: database outage, and the alert that matters most is often "the database is down".
DEFAULT_OUTBOX: Final = Path("logs/alerts.jsonl")


@dataclass(slots=True)
class Alert:
    """One thing worth a human's attention, with everything the escalation template needs."""

    rule: str
    severity: str
    runbook: str
    symptom: str
    evidence: list[str] = field(default_factory=list)
    tier: str = "SAFE"
    action: str = ""
    reversible_with: str = ""
    blast_radius: str = ""
    raised_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "runbook": self.runbook,
            "symptom": self.symptom,
            "evidence": self.evidence,
            "tier": self.tier,
            "action": self.action,
            "reversible_with": self.reversible_with,
            "blast_radius": self.blast_radius,
            "raised_at": self.raised_at,
        }


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _enabled_ids(session: Session) -> set[str]:
    """The sources the monitor can act on.

    Disabling a source is itself a runbook cure ("disable the flaky source for 7 days"), so the
    rules that fire *about* a source must go quiet once it is disabled — otherwise the cure never
    satisfies the alarm and the monitor would keep proposing a change already made.
    """
    return {
        str(row.id)
        for row in session.execute(select(Source).where(Source.enabled.is_(True))).scalars().all()
    }


def _rate_limit_alerts(
    session: Session, *, since: datetime, storm: int, raised_at: str
) -> list[Alert]:
    """Sources that hit rate limits repeatedly: the 429 storm runbook."""
    rows = session.execute(
        select(
            RunSourceLog.source_id,
            func.sum(RunSourceLog.rate_limit_hits).label("hits"),
            func.count().label("runs"),
        )
        .where(RunSourceLog.started_at >= since)
        .group_by(RunSourceLog.source_id)
        .having(func.sum(RunSourceLog.rate_limit_hits) >= storm)
    ).all()
    enabled = _enabled_ids(session)
    alerts: list[Alert] = []
    for row in rows:
        if str(row.source_id) not in enabled:
            continue
        alerts.append(
            Alert(
                rule="rate_limit_storm",
                severity="warn",
                runbook="http-429-storm",
                symptom=(
                    f"{row.source_id} hit rate limits {int(row.hits)} time(s) across "
                    f"{int(row.runs)} run(s) since {since:%Y-%m-%d}"
                ),
                evidence=[
                    f"uv run python -m trend_analyst.health --json  # source {row.source_id}",
                    f"uv run python -m trend_analyst.health --json | jq '.sources[] "
                    f"| select(.id==\"{row.source_id}\")'",
                ],
                tier="SAFE",
                action=(
                    "lower the source's rps in config/sources.yaml and restart the run from its "
                    "checkpoint; if the storm continues, propose disabling it for 7 days"
                ),
                reversible_with="git revert the sources.yaml commit",
                blast_radius=(
                    "a slower source delays its own collection; other sources are unaffected"
                ),
                raised_at=raised_at,
            )
        )
    return alerts


def _quota_alerts(
    session: Session, *, now: datetime, threshold: float, raised_at: str
) -> list[Alert]:
    """Sources past the burn threshold, measured against the budget the registry declares."""
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    spend_rows = session.execute(
        select(QuotaLedger.source_id, func.sum(QuotaLedger.amount))
        .where(QuotaLedger.spend_date >= start_of_day)
        .group_by(QuotaLedger.source_id)
    ).all()
    budgets = {
        str(source.id): int(source.budget_per_day)
        for source in session.execute(select(Source)).scalars().all()
    }
    alerts: list[Alert] = []
    for raw_source_id, spent in spend_rows:
        source_id = str(raw_source_id)
        budget = budgets.get(source_id)
        if budget is None and source_id.startswith("llm_"):
            # Gate spend is capped per gate in code rather than per registry row; the token log is
            # the authority there, and the gateway already refuses past its cap.
            continue
        if not budget:
            continue
        burn = float(spent) / budget
        if burn >= threshold:
            alerts.append(
                Alert(
                    rule="quota_burn",
                    severity="critical" if burn >= 1.0 else "warn",
                    runbook="quota-burn",
                    symptom=f"{source_id} burned {burn:.0%} of its daily budget ({spent}/{budget})",
                    evidence=[
                        "uv run python -m trend_analyst.health --json | jq .quota",
                        "uv run python -m scripts.ttl_job --dry-run  # ledger intact?",
                    ],
                    tier="APPROVAL",
                    action=(
                        "propose a budget change (config/sources.yaml) or let the source skip "
                        "until the next day; do not raise a budget to finish a run"
                    ),
                    reversible_with="git revert the sources.yaml commit",
                    blast_radius=(
                        "a raised budget spends real quota; a lowered one silently narrows coverage"
                    ),
                    raised_at=raised_at,
                )
            )
    return alerts


def _failure_streak_alerts(
    session: Session, *, streak: int, raised_at: str
) -> list[Alert]:
    """Sources whose last N runs failed or degraded: the schema-change runbook."""
    alerts: list[Alert] = []
    for source in session.execute(select(Source)).scalars().all():
        if not source.enabled:
            continue
        recent = session.execute(
            select(RunSourceLog.status, RunSourceLog.reason)
            .where(RunSourceLog.source_id == source.id)
            .order_by(RunSourceLog.started_at.desc())
            .limit(streak)
        ).all()
        if len(recent) < streak:
            continue
        if all(str(row.status) in {"failed", "degraded"} for row in recent):
            reasons = [str(row.reason or "")[:120] for row in recent[:2]]
            alerts.append(
                Alert(
                    rule="source_failure_streak",
                    severity="critical",
                    runbook="source-schema-change",
                    symptom=f"{source.id} failed {len(recent)} run(s) in a row",
                    evidence=[
                        "uv run python -m scripts.sync_sources --dry-run  # registry vs database",
                        f"uv run python -m trend_analyst.pipeline.orchestrator "
                        f"--source {source.id} --fixtures tests/data  # replay the recording",
                        *[f"last reason: {reason}" for reason in reasons if reason],
                    ],
                    tier="APPROVAL",
                    action=(
                        "propose disabling the source for 7 days while its parser is fixed; a "
                        "schema change needs a code change, not a retry"
                    ),
                    reversible_with="git revert the source change; statuses are in git, not the DB",
                    blast_radius=(
                        f"coverage loses {source.id} until it is fixed; the other sources continue"
                    ),
                    raised_at=raised_at,
                )
            )
    return alerts


def _drift_alerts(findings: Iterable[DriftFinding], *, raised_at: str) -> list[Alert]:
    """Turn drift findings into alerts, preserving their tier and reversibility."""
    alerts: list[Alert] = []
    for finding in findings:
        alerts.append(
            Alert(
                rule=finding.kind,
                severity=finding.severity,
                runbook=finding.runbook,
                symptom=finding.summary,
                evidence=[
                    "uv run python -m trend_analyst.monitor.alerts --json",
                    f"detail: {json.dumps(finding.detail, sort_keys=True)}",
                ],
                tier="APPROVAL" if finding.severity == "critical" else "SAFE",
                action=finding.suggested_action,
                reversible_with=finding.reversible_with,
                blast_radius=(
                    "a quality or judgement shift propagates into every later stage: scores, "
                    "briefs and the eval baseline"
                ),
                raised_at=raised_at,
            )
        )
    return alerts


def collect_alerts(
    session: Session,
    *,
    now: datetime | None = None,
    window_hours: int = 48,
    quota_threshold: float = DEFAULT_QUOTA_BURN_THRESHOLD,
    storm: int = DEFAULT_RATE_LIMIT_STORM,
    failure_streak: int = DEFAULT_FAILURE_STREAK,
) -> list[Alert]:
    """Every alert the current state justifies, most severe first."""
    reference = now or datetime.now(UTC)
    raised_at = reference.isoformat()
    since = reference - timedelta(hours=window_hours)

    alerts: list[Alert] = []
    alerts.extend(_quota_alerts(session, now=reference, threshold=quota_threshold,
                                raised_at=raised_at))
    alerts.extend(_rate_limit_alerts(session, since=since, storm=storm, raised_at=raised_at))
    alerts.extend(_failure_streak_alerts(session, streak=failure_streak, raised_at=raised_at))
    drift = [
        finding
        for finding in (
            keep_rate_drift(session, now=reference),
            *watermark_drift(session, now=reference),
            *eval_baseline_drop(session),
        )
        if finding is not None
    ]
    alerts.extend(_drift_alerts(drift, raised_at=raised_at))

    order = {"critical": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda alert: (order.get(alert.severity, 3), alert.rule))
    return alerts


def write_outbox(
    alerts: Sequence[Alert],
    *,
    path: Path = DEFAULT_OUTBOX,
    append: bool = True,
) -> int:
    """Append the alerts to the monitor's inbox. Returns how many lines were written.

    Appending by default: the outbox is a log of what was raised, and a monitor that rewrites it
    loses the history that tells a human whether an alert is new or the fifth night in a row.
    """
    if not alerts:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        for alert in alerts:
            handle.write(json.dumps(alert.as_dict(), sort_keys=True) + "\n")
    return len(alerts)


def load_outbox(*, path: Path = DEFAULT_OUTBOX, limit: int = 50) -> list[Alert]:
    """Read the newest alerts from the inbox (oldest first among the newest ``limit``)."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    alerts: list[Alert] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        alerts.append(
            Alert(
                rule=str(payload.get("rule", "")),
                severity=str(payload.get("severity", "")),
                runbook=str(payload.get("runbook", "")),
                symptom=str(payload.get("symptom", "")),
                evidence=[str(item) for item in payload.get("evidence", [])],
                tier=str(payload.get("tier", "SAFE")),
                action=str(payload.get("action", "")),
                reversible_with=str(payload.get("reversible_with", "")),
                blast_radius=str(payload.get("blast_radius", "")),
                raised_at=str(payload.get("raised_at", "")),
            )
        )
    return alerts


def render_alerts(alerts: Sequence[Alert]) -> str:
    """The text a monitor agent (or a human) reads: symptom, tier, action, reversal."""
    if not alerts:
        return "no alerts: the system is inside every band it watches"
    lines = [f"{len(alerts)} alert(s):"]
    for alert in alerts:
        lines.append(
            f"  [{alert.severity.upper():<8}] {alert.rule} -> runbook {alert.runbook}: "
            f"{alert.symptom}"
        )
        lines.append(f"      tier: {alert.tier} · action: {alert.action}")
        lines.append(f"      reversible: {alert.reversible_with}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: collect the alerts, optionally write them to the outbox, print what a human needs."""
    parser = argparse.ArgumentParser(
        prog="python -m trend_analyst.monitor.alerts",
        description="Collect monitor alerts and optionally append them to the outbox.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write", action="store_true", help="append them to the outbox file")
    parser.add_argument("--path", default=str(DEFAULT_OUTBOX))
    args = parser.parse_args(argv)


    try:
        load_settings(default_config_dir())
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    try:
        engine = create_db_engine()
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}", file=sys.stderr)
        return 1

    sessions = create_session_factory(engine)
    try:
        with sessions() as session:
            alerts = collect_alerts(session)
        written = write_outbox(alerts, path=Path(args.path)) if args.write else 0
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps([alert.as_dict() for alert in alerts], indent=2, default=str))
    else:
        print(render_alerts(alerts))
        if args.write:
            print(f"appended {written} alert(s) to {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
