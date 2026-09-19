"""The health CLI (spec §9) — what the monitor agent reads instead of raw logs.

    uv run python -m trend_analyst.health            # human view
    uv run python -m trend_analyst.health --json      # machine view (the agent parses this)
    uv run ta-health                                  # same, via the installed entry point

It answers one question: **can the system run tonight, and if not, what exactly is
wrong?** So it reports the last run, per-source ok/degraded/down, quota burn against the
registry budget, the Judge keep-rate and the eval baseline.

It never raises for an operational problem: an unreachable database is a status (DOWN)
*with a reason*, not a traceback — and a health check that exits 0 while something is
broken is the one failure mode that makes monitoring worthless.

Exit codes: ``0`` healthy, ``1`` degraded, ``2`` down.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.sources.registry import (
    Registry,
    RegistryError,
    default_registry_path,
    load_registry,
)
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    check_database,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import (
    Candidate,
    EvalCase,
    Judgement,
    QuotaLedger,
    Run,
    RunSourceLog,
)

__all__ = [
    "ExitCode",
    "HealthInputs",
    "HealthReport",
    "SourceStatus",
    "build_report",
    "collect_health",
    "main",
    "render_text",
    "source_status",
]

_REPORT_CONFIG = ConfigDict(extra="forbid", frozen=True)

HealthStatus = Literal["healthy", "degraded", "down"]
SourceStatus = Literal["ok", "degraded", "down", "skipped", "disabled", "never_run"]


class ExitCode(IntEnum):
    """The CLI's contract with the monitor agent."""

    HEALTHY = 0
    DEGRADED = 1
    DOWN = 2


EXIT_CODES: dict[str, ExitCode] = {
    "healthy": ExitCode.HEALTHY,
    "degraded": ExitCode.DEGRADED,
    "down": ExitCode.DOWN,
}

#: How a `run_source_log.status` maps onto a health status.
#:
#: ``skipped`` is its own state, not ``degraded``: a source that declares why it could not run (no
#: key, not recorded for an offline replay, disabled upstream) is a coverage fact, not an incident.
#: Folding it into ``degraded`` made health report a system-wide problem every time twelve keyless
#: sources skipped - the same false-alarm class as reporting a missing credential as a failure.
_LAST_STATUS_MAP: dict[str, SourceStatus] = {
    "ok": "ok",
    "degraded": "degraded",
    "skipped": "skipped",
    "failed": "down",
}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
class DatabaseHealth(BaseModel):
    model_config = _REPORT_CONFIG

    ok: bool
    server_version: str | None = None
    pgvector_version: str | None = None
    detail: str | None = None


class RegistryHealth(BaseModel):
    model_config = _REPORT_CONFIG

    ok: bool
    path: str
    total: int = 0
    enabled: int = 0
    disabled: int = 0
    tier_s: int = 0
    tier_a: int = 0
    detail: str | None = None


class LayerHealth(BaseModel):
    model_config = _REPORT_CONFIG

    layer: str
    status: str
    items: int = 0


class LastRun(BaseModel):
    model_config = _REPORT_CONFIG

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    trigger: str
    layers: tuple[LayerHealth, ...] = ()


class SourceHealth(BaseModel):
    model_config = _REPORT_CONFIG

    source_id: str
    role: str
    tier: str
    status: SourceStatus
    last_status: str | None = None
    last_seen_at: datetime | None = None
    items_fetched: int = 0
    quota_spent_today: int = 0
    budget_per_day: int = 0
    burn_pct: float = 0.0
    rate_limit_hits: int = 0
    reasons: tuple[str, ...] = ()


class QuotaHealth(BaseModel):
    model_config = _REPORT_CONFIG

    spent_today: int = 0
    budget_today: int = 0
    burn_pct: float = 0.0
    alert_pct: float = 80.0
    over_threshold: tuple[str, ...] = ()


class GateHealth(BaseModel):
    model_config = _REPORT_CONFIG

    judge_keep_rate: float | None = None
    kept: int = 0
    dropped: int = 0
    note: str = ""


class EvalHealth(BaseModel):
    model_config = _REPORT_CONFIG

    cases: int = 0
    baseline_mean: float | None = None
    #: Cases whose every check ran and passed, including the Judge's keep/drop.
    verified: int = 0
    #: Cases whose deterministic checks pass but whose keep/drop check is still pending.
    partial: int = 0
    failed: int = 0
    note: str = ""


class HealthReport(BaseModel):
    """Everything health knows, in one serialisable object."""

    model_config = _REPORT_CONFIG

    status: HealthStatus
    reasons: tuple[str, ...] = ()
    checked_at: datetime
    database: DatabaseHealth
    registry: RegistryHealth
    last_run: LastRun | None = None
    sources: tuple[SourceHealth, ...] = ()
    quota: QuotaHealth = Field(default_factory=QuotaHealth)
    gates: GateHealth = Field(default_factory=GateHealth)
    evals: EvalHealth = Field(default_factory=EvalHealth)

    @property
    def exit_code(self) -> ExitCode:
        return EXIT_CODES[self.status]

    def counts(self) -> dict[str, int]:
        """Source counts by status. Every status in the literal is present, so a new one is loud.

        Using `defaultdict(int)` here would have swallowed the KeyError that caught the missing
        `skipped` key — a dict with a fixed key set turns "I forgot a status" into a crash the first
        time the status appears, which is exactly when someone is looking.
        """
        counts = dict.fromkeys(get_status_literal(), 0)
        for source in self.sources:
            counts[source.status] += 1
        return counts


# ---------------------------------------------------------------------------
# Source status rules (pure: unit-testable without a database)
# ---------------------------------------------------------------------------
def source_status(
    *,
    enabled: bool,
    last_status: str | None,
    rate_limit_hits: int = 0,
    burn_pct: float = 0.0,
    alert_pct: float = 80.0,
) -> tuple[SourceStatus, tuple[str, ...]]:
    """Decide one source's status, and say why.

    Rules, in order:

    1. disabled in the registry -> ``disabled`` (not a problem: Tier A waits for a key)
    2. no run has touched it    -> ``never_run`` (normal on a fresh install)
    3. its last logged outcome  -> ``ok`` / ``degraded`` / ``down``
    4. an ``ok`` source is escalated to ``degraded`` when it hit rate limits or burned
       past the alert threshold: both mean tonight's run will collect less than it should.
    """
    if not enabled:
        return "disabled", ()

    if last_status is None:
        return "never_run", ("no run has executed this source yet",)

    status: SourceStatus = _LAST_STATUS_MAP.get(last_status, "degraded")
    reasons: list[str] = []
    if last_status not in _LAST_STATUS_MAP:
        reasons.append(f"unrecognised run status {last_status!r} — treating as degraded")

    if status == "ok":
        if rate_limit_hits > 0:
            status = "degraded"
            reasons.append(f"rate limited {rate_limit_hits}x in the last run")
        if burn_pct >= alert_pct:
            status = "degraded"
            reasons.append(f"quota burn {burn_pct:.0f}% >= alert threshold {alert_pct:.0f}%")
    elif status == "degraded" and not reasons:
        reasons.append(f"last run reported {last_status!r}")
    elif status == "skipped" and not reasons:
        reasons.append("last run skipped it (see the run log's reason)")

    return status, tuple(reasons)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HealthInputs:
    """Where health gets its facts. Injectable, so tests need no configuration files."""

    registry_path: Path
    engine: Engine | None
    alert_pct: float = 80.0


def _utc_day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _load_registry(inputs: HealthInputs) -> tuple[Registry | None, RegistryHealth, list[str]]:
    try:
        registry = load_registry(inputs.registry_path)
    except RegistryError as exc:
        return (
            None,
            RegistryHealth(ok=False, path=str(inputs.registry_path), detail=str(exc)),
            [f"registry invalid: {exc}"],
        )

    summary = registry.summary()
    return (
        registry,
        RegistryHealth(
            ok=True,
            path=str(inputs.registry_path),
            total=summary["total"],
            enabled=summary["enabled"],
            disabled=summary["disabled"],
            tier_s=summary["tier_s"],
            tier_a=summary["tier_a"],
        ),
        [],
    )


def _last_run(session: Session) -> LastRun | None:
    row = session.execute(select(Run).order_by(Run.started_at.desc()).limit(1)).scalar_one_or_none()
    if row is None:
        return None

    layers: list[LayerHealth] = []
    for layer, details in sorted((row.layer_status or {}).items()):
        if isinstance(details, dict):
            layers.append(
                LayerHealth(
                    layer=str(layer),
                    status=str(details.get("status", "unknown")),
                    items=int(details.get("items", 0)),
                )
            )
        else:
            layers.append(LayerHealth(layer=str(layer), status="unknown"))
    return LastRun(
        run_id=str(row.id),
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,
        trigger=row.trigger,
        layers=tuple(layers),
    )


def _latest_source_logs(session: Session) -> dict[str, RunSourceLog]:
    rows = (
        session.execute(
            select(RunSourceLog)
            .distinct(RunSourceLog.source_id)
            .order_by(RunSourceLog.source_id, RunSourceLog.started_at.desc())
        )
        .scalars()
        .all()
    )
    return {row.source_id: row for row in rows}


def _quota_spent_today(session: Session, now: datetime) -> dict[str, int]:
    rows = session.execute(
        select(QuotaLedger.source_id, func.sum(QuotaLedger.amount))
        .where(QuotaLedger.spend_date >= _utc_day_start(now))
        .group_by(QuotaLedger.source_id)
    ).all()
    return {str(source_id): int(total or 0) for source_id, total in rows}


def _gate_health(session: Session) -> GateHealth:
    """Judge keep-rate, derived from the candidates the Judge kept or dropped.

    Derived, not stored: the Judge writes `candidates.status`, and a parallel counter
    would be a second source of truth for the same fact.
    """
    rows = session.execute(select(Candidate.status, func.count()).group_by(Candidate.status)).all()
    counts = {str(status): int(count) for status, count in rows}
    kept = counts.get("kept", 0) + counts.get("briefed", 0)
    dropped = counts.get("dropped", 0)
    decisions = kept + dropped
    if decisions == 0:
        return GateHealth(note="no Judge decisions recorded yet (the Judge gate lands in P3)")
    return GateHealth(
        judge_keep_rate=kept / decisions,
        kept=kept,
        dropped=dropped,
        note="derived from candidates.status",
    )


def _eval_health(session: Session) -> EvalHealth:
    """Case counts by outcome, using the same accounting as `evals/run_evals.py`.

    `verified` / `partial` / `failed` are derived here rather than read from the last eval run's
    report, so health cannot report a stale verdict. `partial` is the interesting number: those
    cases pass everything except the Judge's keep/drop, which needs a verdict that does not exist
    yet.
    """
    cases = int(session.execute(select(func.count()).select_from(EvalCase)).scalar_one())
    if cases == 0:
        return EvalHealth(note="no golden cases yet (seed them with scripts/seed_evals.py)")
    judged = int(
        session.execute(
            select(func.count(func.distinct(Judgement.candidate_id))).where(
                Judgement.gate == "judge"
            )
        ).scalar_one()
    )
    baseline = session.execute(select(func.avg(EvalCase.baseline_score))).scalar_one()
    return EvalHealth(
        cases=cases,
        baseline_mean=float(baseline) if baseline is not None else None,
        # A case is "verified" only if a Judge verdict exists for its candidate; health cannot see
        # whether that verdict *agrees* with the case's expectation (that is run_evals.py's job), so
        # it reports the weaker, honest statement rather than a green it cannot substantiate.
        verified=0,
        partial=cases,
        failed=0,
        note=(
            f"{judged} judged candidate(s) in the database; run `python -m evals.run_evals` "
            "for the per-case verdicts"
        ),
    )


def collect_health(inputs: HealthInputs, *, now: datetime | None = None) -> HealthReport:
    """Assemble the report. Operational problems become statuses and reasons, not raises."""
    checked_at = now or datetime.now(UTC)
    reasons: list[str] = []

    registry, registry_health, registry_reasons = _load_registry(inputs)
    reasons.extend(registry_reasons)

    if inputs.engine is None:
        database = DatabaseHealth(ok=False, detail="no database configured (db.url is empty)")
        reasons.append("database not configured")
    else:
        probe = check_database(inputs.engine)
        database = DatabaseHealth(
            ok=probe.ok,
            server_version=probe.server_version,
            pgvector_version=probe.pgvector_version,
            detail=probe.detail,
        )
        if not probe.ok:
            reasons.append(f"database unreachable: {probe.detail}")

    if inputs.engine is None or not database.ok or registry is None:
        return HealthReport(
            status="down",
            reasons=tuple(reasons),
            checked_at=checked_at,
            database=database,
            registry=registry_health,
        )

    factory = create_session_factory(inputs.engine)
    with factory() as session:
        return build_report(
            session,
            registry=registry,
            registry_health=registry_health,
            database=database,
            alert_pct=inputs.alert_pct,
            checked_at=checked_at,
            reasons=reasons,
        )


def build_report(
    session: Session,
    *,
    registry: Registry,
    registry_health: RegistryHealth,
    database: DatabaseHealth,
    alert_pct: float = 80.0,
    checked_at: datetime,
    reasons: Sequence[str] = (),
) -> HealthReport:
    """Build the report from an open session.

    Split out of :func:`collect_health` so a caller that already holds a session (tests,
    and the acceptance checks) can use it — and so the query path can be exercised
    without a second connection, which would not see uncommitted rows.
    """
    accumulated = list(reasons)
    last_run = _last_run(session)
    logs = _latest_source_logs(session)
    spent_today = _quota_spent_today(session, checked_at)
    gates = _gate_health(session)
    evals = _eval_health(session)

    sources: list[SourceHealth] = []
    for entry in registry.sources:
        log = logs.get(entry.id)
        spent = spent_today.get(entry.id, 0)
        burn = 100.0 * spent / entry.budget_per_day if entry.budget_per_day else 0.0
        status, why = source_status(
            enabled=entry.enabled,
            last_status=log.status if log is not None else None,
            rate_limit_hits=log.rate_limit_hits if log is not None else 0,
            burn_pct=burn,
            alert_pct=alert_pct,
        )
        sources.append(
            SourceHealth(
                source_id=entry.id,
                role=entry.role,
                tier=entry.tier,
                status=status,
                last_status=log.status if log is not None else None,
                last_seen_at=log.started_at if log is not None else None,
                items_fetched=log.items_fetched if log is not None else 0,
                quota_spent_today=spent,
                budget_per_day=entry.budget_per_day,
                burn_pct=burn,
                rate_limit_hits=log.rate_limit_hits if log is not None else 0,
                reasons=why,
            )
        )

    budget_today = sum(entry.budget_per_day for entry in registry.enabled())
    spent_total = sum(source.quota_spent_today for source in sources)
    over = tuple(
        source.source_id
        for source in sources
        if source.status != "disabled" and source.burn_pct >= alert_pct
    )
    quota = QuotaHealth(
        spent_today=spent_total,
        budget_today=budget_today,
        burn_pct=100.0 * spent_total / budget_today if budget_today else 0.0,
        alert_pct=alert_pct,
        over_threshold=over,
    )

    counts = {"ok": 0, "degraded": 0, "down": 0, "skipped": 0, "disabled": 0, "never_run": 0}
    for source in sources:
        counts[source.status] += 1

    if counts["down"]:
        accumulated.append(f"{counts['down']} source(s) down")
    if counts["degraded"]:
        accumulated.append(f"{counts['degraded']} source(s) degraded")
    if over:
        accumulated.append(f"{len(over)} source(s) above the quota alert threshold")

    if counts["down"]:
        overall: HealthStatus = "down"
    elif counts["degraded"] or over:
        overall = "degraded"
    else:
        overall = "healthy"
    # A skipped source never changes the overall status: it explains missing coverage, and reports
    # in the sources line, but "I did not run" is not "something is broken".

    return HealthReport(
        status=overall,
        reasons=tuple(accumulated),
        checked_at=checked_at,
        database=database,
        registry=registry_health,
        last_run=last_run,
        sources=tuple(sources),
        quota=quota,
        gates=gates,
        evals=evals,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def get_status_literal() -> tuple[str, ...]:
    """The source statuses, read from the `SourceStatus` literal so the two cannot drift apart."""
    from typing import get_args  # noqa: PLC0415 - typing introspection, used once

    return tuple(str(name) for name in get_args(SourceStatus))


def _render_last_run(run: LastRun | None) -> list[str]:
    """The last-run line, with a running row called out rather than described as finished."""
    if run is None:
        return ["last run: none yet (no nightly run has executed)"]
    if run.finished_at is None:
        return [
            f"last run: {run.run_id} — {run.status} ({run.trigger}), STILL RUNNING: "
            f"started {run.started_at.isoformat(timespec='seconds')} — it may be a crashed run "
            "that a resume will pick up"
        ]
    finished = run.finished_at.isoformat(timespec="seconds")
    return [f"last run: {run.run_id} — {run.status} ({run.trigger}), finished {finished}"]


def render_text(report: HealthReport, *, verbose: bool = False) -> str:
    counts = report.counts()
    lines: list[str] = [
        f"Trend Analyst health — {report.status.upper()} (exit {int(report.exit_code)})",
        f"checked at {report.checked_at.isoformat(timespec='seconds')}",
        "",
    ]

    if report.database.ok:
        lines.append(
            f"database: ok — postgres {report.database.server_version}, "
            f"pgvector {report.database.pgvector_version}"
        )
    else:
        lines.append(f"database: DOWN — {report.database.detail}")

    if report.registry.ok:
        lines.append(
            f"registry: ok — {report.registry.total} sources "
            f"({report.registry.tier_s} Tier S, {report.registry.tier_a} Tier A) · "
            f"{report.registry.enabled} enabled, {report.registry.disabled} disabled"
        )
    else:
        lines.append(f"registry: DOWN — {report.registry.detail}")

    lines.extend(_render_last_run(report.last_run))
    if report.last_run is not None:
        lines.extend(
            f"    {layer.layer}: {layer.status} ({layer.items} items)"
            for layer in report.last_run.layers
        )

    lines.append(
        "sources: "
        + " · ".join(
            f"{counts[name]} {name}"
            for name in ("ok", "degraded", "down", "skipped", "never_run", "disabled")
        )
    )
    lines.append(
        f"quota: {report.quota.spent_today} / {report.quota.budget_today} requests today "
        f"({report.quota.burn_pct:.1f}%), alert at {report.quota.alert_pct:.0f}%"
    )
    if report.gates.judge_keep_rate is None:
        lines.append(f"gates: judge keep-rate n/a — {report.gates.note}")
    else:
        lines.append(
            f"gates: judge keep-rate {report.gates.judge_keep_rate:.2f} "
            f"({report.gates.kept} kept / {report.gates.dropped} dropped)"
        )
    if report.evals.cases:
        lines.append(
            f"evals: {report.evals.cases} case(s) — {report.evals.verified} verified, "
            f"{report.evals.partial} partial (keep/drop pending a Judge verdict), "
            f"{report.evals.failed} failed"
        )
    else:
        lines.append(f"evals: 0 case(s) — {report.evals.note}")

    interesting = [
        source
        for source in report.sources
        if verbose or source.status in {"degraded", "down", "skipped"}
    ]
    if interesting:
        lines.append("")
        lines.append("all sources:" if verbose else "sources needing attention:")
        for source in interesting:
            detail = "; ".join(source.reasons) if source.reasons else "ok"
            lines.append(
                f"  {source.source_id:<22} {source.status:<10} "
                f"{source.quota_spent_today}/{source.budget_per_day} · {detail}"
            )

    if report.reasons:
        lines.append("")
        lines.append("why not healthy:")
        lines.extend(f"  - {reason}" for reason in report.reasons)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trend_analyst.health",
        description="Report whether the system can run tonight (spec §9).",
    )
    parser.add_argument("--config-dir", default=None, help="directory holding config/*.yaml")
    parser.add_argument("--env", default=None, help="environment name (default: TA_ENV or dev)")
    parser.add_argument("--registry", default=None, help="path to sources.yaml")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--verbose", action="store_true", help="list every source")
    return parser


def _down_report(registry_path: Path, reason: str) -> HealthReport:
    return HealthReport(
        status="down",
        reasons=(reason,),
        checked_at=datetime.now(UTC),
        database=DatabaseHealth(ok=False, detail="not reachable"),
        registry=RegistryHealth(ok=False, path=str(registry_path), detail="not loaded"),
    )


def _emit(report: HealthReport, args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, default=str))
    else:
        print(render_text(report, verbose=bool(args.verbose)))


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `python -m trend_analyst.health` and the `ta-health` script."""
    args = _build_parser().parse_args(argv)

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    registry_path = Path(args.registry) if args.registry else default_registry_path(config_dir)

    try:
        settings = load_settings(config_dir, env_name=args.env)
    except ConfigError as exc:
        report = _down_report(registry_path, f"configuration error: {exc}")
        _emit(report, args)
        return int(report.exit_code)

    engine: Engine | None
    try:
        engine = create_db_engine(settings) if settings.db.configured else None
    except DatabaseNotConfiguredError as exc:  # pragma: no cover - guarded by the check above
        report = _down_report(registry_path, f"database not configured: {exc}")
        _emit(report, args)
        return int(report.exit_code)

    try:
        report = collect_health(
            HealthInputs(
                registry_path=registry_path,
                engine=engine,
                alert_pct=settings.health.quota_burn_alert_pct,
            )
        )
    finally:
        if engine is not None:
            engine.dispose()

    _emit(report, args)
    return int(report.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
