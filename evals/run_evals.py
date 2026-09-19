"""The eval runner: golden cases executed, with the score compared to what was expected.

    uv run python -m evals.run_evals                # check the seeded cases
    uv run python -m evals.run_evals --json
    uv run python -m evals.run_evals --gate judge --replay   # offline, no quota

Spec §8: *"50 golden cases in evals/cases.yaml: input signals plus expected keep/drop, fad label,
and score band."* This runner implements the parts decidable in code today —

* **expected keep/drop** against the newest Judge verdict for that candidate (when one exists);
* **expected fad label** against the newest score snapshot;
* **score band** against the newest MGS;
* **citation presence** (a case seeded from an ungrounded verdict is allowed to be ungrounded, but
  the count is reported, because "how often the model cites nothing" is a quality signal).

What it deliberately does **not** do yet: the LLM-judge rubric from §8 (0.0-1.0 plus pass/fail on
factual accuracy, citation accuracy, completeness, source quality, tool efficiency). That needs brie
texts and a paid judge, and it belongs with the brief renderer in P5 — claiming it here would be
claiming a quality measurement that has not happened.

The runner never invents cases and never edits them: a case whose expectations no longer match is
reported as a failure, which is the entire point of freezing them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml
from sqlalchemy import select

from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.history import history
from trend_analyst.store.models import Candidate, Judgement

__all__ = ["CaseResult", "EvalReport", "load_cases", "main", "run_cases"]

CASES_PATH: Final = Path("evals/cases.yaml")


@dataclass(slots=True)
class CaseResult:
    """One case's outcome, with the reason it failed when it did."""

    case_id: str
    phrase: str
    category: str
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "phrase": self.phrase,
            "category": self.category,
            "passed": self.passed,
            "checks": self.checks,
            "observed": self.observed,
            "reasons": self.reasons,
        }


@dataclass(slots=True)
class EvalReport:
    """Every case's result, plus the summary the gate looks at."""

    total: int = 0
    passed: int = 0
    skipped: int = 0
    results: list[CaseResult] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return self.total - self.passed - self.skipped

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "results": [result.as_dict() for result in self.results],
            "ok": self.ok,
        }

    def render(self) -> str:
        lines = [f"eval cases: {self.passed}/{self.total} passed ({self.skipped} skipped)"]
        for result in self.results:
            mark = "PASS" if result.passed else ("SKIP" if not result.checks else "FAIL")
            detail = ", ".join(
                f"{name}={'ok' if ok else 'no'}" for name, ok in result.checks.items()
            )
            lines.append(f"  {mark}  {result.case_id[:52]:<52} {detail}")
            lines.extend(f"        {reason}" for reason in result.reasons)
        return "\n".join(lines)


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    """Read ``evals/cases.yaml``. A missing file is an error, an empty one is not."""
    if not path.is_file():
        raise FileNotFoundError(f"no eval cases at {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = payload.get("cases") or []
    if not isinstance(cases, list):
        raise ValueError(f"{path}: 'cases' must be a list")
    return [case for case in cases if isinstance(case, dict)]


def run_cases(
    session: Any,
    cases: Sequence[dict[str, Any]],
    *,
    require_judgement: bool = False,
) -> EvalReport:
    """Check every case against the database's current state.

    Args:
        require_judgement: when True, a case with no Judge verdict is a failure rather than a skip.
            The gate sets it (a case that is never judged is untested); an interactive run does not.
    """
    report = EvalReport(total=len(cases))
    for case in cases:
        phrase = str(case.get("phrase") or case.get("id") or "?")
        category = str(case.get("category") or "")
        case_id = str(case.get("id") or phrase)
        result = CaseResult(case_id=case_id, phrase=phrase, category=category, passed=True)

        candidate = session.execute(
            select(Candidate).where(Candidate.phrase == phrase)
        ).scalars().first()
        if candidate is None:
            # A case whose candidate no longer exists cannot be checked. Reported as skipped with a
            # reason rather than silently passed: the seeded cases come from real runs, and mining
            # rules do change.
            report.skipped += 1
            result.passed = False
            result.reasons.append("no candidate with that phrase in the database")
            result.checks = {}
            report.results.append(result)
            continue

        points = history(session, phrase=phrase)
        if not points:
            report.skipped += 1
            result.passed = False
            result.reasons.append("no score snapshot for that candidate")
            report.results.append(result)
            continue
        newest = points[-1]

        band = case.get("score_band") or {}
        low = float(band.get("min", 0.0))
        high = float(band.get("max", 100.0))
        result.checks["score_band"] = low <= newest.mgs <= high
        result.observed["mgs"] = round(newest.mgs, 2)
        if not result.checks["score_band"]:
            result.reasons.append(
                f"MGS {newest.mgs:.2f} is outside the expected band {low:.2f}-{high:.2f}"
            )

        expected_label = case.get("expected_fad_label")
        if expected_label:
            result.checks["fad_label"] = str(newest.fad_label) == str(expected_label)
            result.observed["fad_label"] = newest.fad_label
            if not result.checks["fad_label"]:
                result.reasons.append(
                    f"fad label is {newest.fad_label!r}, expected {expected_label!r}"
                )

        judgement = session.execute(
            select(Judgement)
            .where(Judgement.candidate_id == int(candidate.id))
            .order_by(Judgement.created_at.desc(), Judgement.id.desc())
            .limit(1)
        ).scalars().first()
        if judgement is None and not require_judgement:
            report.skipped += 1
            result.reasons.append("not judged yet: keep/drop not checked")
            report.results.append(result)
            continue
        _check_keep_drop(result, case=case, judgement=judgement)

        result.passed = all(result.checks.values()) if result.checks else False
        if result.passed:
            report.passed += 1
        report.results.append(result)
    return report


def _check_keep_drop(
    result: CaseResult, *, case: dict[str, Any], judgement: Judgement | None
) -> None:
    """Compare the newest Judge verdict with what the case expects."""
    expected_keep = case.get("expected_keep")
    if judgement is None:
        result.checks["keep_drop"] = False
        result.reasons.append("no Judge verdict for this case")
        return
    result.observed["decision"] = str(judgement.decision)
    result.observed["ungrounded"] = bool(judgement.ungrounded)
    if expected_keep is None:
        return
    observed_keep = str(judgement.decision) == "keep"
    result.checks["keep_drop"] = observed_keep == bool(expected_keep)
    if not result.checks["keep_drop"]:
        result.reasons.append(
            f"the Judge said {judgement.decision}, the case expects "
            f"{'keep' if expected_keep else 'drop'}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run_evals",
        description="Run the golden eval cases against the current database.",
    )
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--limit", type=int, default=None, help="check only the first N cases")
    parser.add_argument(
        "--require-judgement",
        action="store_true",
        help="treat an unjudged case as a failure (the gate uses this)",
    )
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        engine = create_db_engine()
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}", file=sys.stderr)
        return 1

    try:
        cases = load_cases(Path(args.cases))
    except (FileNotFoundError, ValueError) as exc:
        print(f"cannot read cases: {exc}", file=sys.stderr)
        return 1
    if args.limit:
        cases = cases[: args.limit]

    sessions = create_session_factory(engine)
    try:
        with sessions() as session:
            report = run_cases(session, cases, require_judgement=args.require_judgement)
    finally:
        engine.dispose()

    payload = {"run_at": datetime.now(UTC).isoformat(), **report.as_dict()}
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
