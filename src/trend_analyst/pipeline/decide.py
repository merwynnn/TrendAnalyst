"""The decide run: L1 mining then L3 scoring, in one resumable pass (brief P2).

Lives beside `orchestrator.py` rather than inside it because the two runs have different
shapes: L0's unit of work is a *source* (with a budget, a watermark and a ledger row), while
L1+L3's unit of work is the whole lake window, for which "resume" means something simpler —
a candidate already scored in this run with this weights version is not scored again, which
the snapshot's unique key enforces in the database rather than in a flag.

The run is deliberately one transaction per phase instead of per candidate: a snapshot is
small, the layers are idempotent, and a crash mid-way re-does bounded work. Where that would
be wrong (L2's Tier-A budget) the ledger exists — and L2 does not use this path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from config.categories import Taxonomy, default_taxonomy
from trend_analyst.pipeline.layers.l1 import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MIN_KEEP,
    DEFAULT_PRUNE_FRACTION,
    MiningReport,
    mine,
)
from trend_analyst.pipeline.layers.l3 import (
    L3Report,
    ScoredCandidate,
    load_attention_points,
    score_phrases,
)
from trend_analyst.pipeline.state import finish_run, start_run
from trend_analyst.scoring.mgs import WEIGHTS_V1, MgSWeights
from trend_analyst.store.snapshots import (
    RankedScore,
    ScoreRecord,
    candidate_key,
    latest_ranked,
    snapshot_hash,
    upsert_candidates,
    write_snapshots,
)

__all__ = ["DecideReport", "run_decide"]


@dataclass(slots=True)
class DecideReport:
    """The L1+L3 outcome: what was mined, what survived, what was written."""

    run_id: uuid.UUID
    status: str
    mining: MiningReport
    scoring: L3Report
    snapshots_written: int = 0
    snapshots_existing: int = 0
    snapshot_hash: str = ""
    top: tuple[ScoredCandidate, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def candidates(self) -> tuple[ScoredCandidate, ...]:
        return self.scoring.candidates

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "status": self.status,
            "mining": {
                "texts": self.mining.texts_scanned,
                "ngrams": self.mining.ngrams,
                "unmatched": self.mining.unmatched,
                "stale": self.mining.stale,
                "mined": self.mining.mined,
                "pruned": self.mining.pruned,
                "kept": self.mining.kept,
                "prune_fraction": self.mining.prune_fraction,
                "min_keep": self.mining.min_keep,
            },
            "scoring": self.scoring.as_dict(),
            "snapshots_written": self.snapshots_written,
            "snapshots_existing": self.snapshots_existing,
            "snapshot_hash": self.snapshot_hash,
            "top": [candidate.as_dict() for candidate in self.top],
        }

    def render(self, top: int = 10) -> str:
        """The ranked table — the acceptance criterion of P2, printed."""
        lines = [
            f"decide run {self.run_id} · status {self.status}",
            self.mining.summary(),
            self.scoring.summary(),
            f"snapshots: {self.snapshots_written} new, {self.snapshots_existing} already present",
            f"snapshot hash: {self.snapshot_hash[:16]}",
            "",
            _render_table(self.top[:top]),
        ]
        return "\n".join(lines)


#: Column widths chosen for a terminal, not a paper: the histogram of DV/SS/MP makes the
#: shape of a candidate readable at a glance, which is the entire point of a ranked table.
def _render_table(candidates: tuple[ScoredCandidate, ...]) -> str:
    """Fixed-width table: MGS, sub-scores, fad, monthly revenue range, phrase."""
    if not candidates:
        return "(no candidates survived mining)"

    header = (
        f"{'MGS':>5}  {'DV':>4} {'SS':>4} {'SP':>4} {'MP':>4} {'FE':>4}  "
        f"{'fad':>9}  {'$/mo (P10-P50-P90)':>26}  phrase"
    )
    rule = "-" * len(header)
    rows = [header, rule]
    for candidate in candidates:
        subs = candidate.result.sub_scores
        flag = f"{candidate.fad.label}"
        if candidate.result.small_sample:
            flag += "*"
        money = (
            f"{candidate.revenue.p10:,.0f}-{candidate.revenue.p50:,.0f}"
            f"-{candidate.revenue.p90:,.0f}"
        )
        rows.append(
            f"{candidate.mgs:5.1f}  {subs.dv:4.0f} {subs.ss:4.0f} {subs.sp:4.0f} "
            f"{subs.mp:4.0f} {subs.fe:4.0f}  {flag:>9}  {money:>26}  "
            f"{candidate.phrase} [{candidate.category_id}]"
        )
    rows.append(rule)
    rows.append(
        "* category has fewer than 5 candidates: its percentiles are direction, not precision"
    )
    return "\n".join(rows)


def run_decide(
    *,
    sessions: sessionmaker[Session],
    taxonomy: Taxonomy | None = None,
    as_of: datetime,
    weights: MgSWeights = WEIGHTS_V1,
    trigger: str = "manual",
    window_days: int = 365,
    prune_fraction: float = DEFAULT_PRUNE_FRACTION,
    min_keep: int = DEFAULT_MIN_KEEP,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    source_ids: list[str] | None = None,
    dry_run: bool = False,
    top: int = 10,
) -> DecideReport:
    """Mine the lake window, score what survives, and append the score snapshots.

    Args:
        as_of: the reference instant for every window. Passed in so a replay over the same
            lake reproduces the same snapshots (spec §5.4) — the CLI pins it to the newest
            signal when running offline against fixtures.
        min_keep: the mining floor; the run's note records it next to the prune fraction so a
            reader can see the prune was a decision, not an accident.
        dry_run: do everything and write nothing — including the candidates, so a dry run
            leaves the database exactly as it found it.
    """
    taxonomy = taxonomy or default_taxonomy()

    with sessions() as session:
        handle = start_run(session, trigger=trigger, resume=False)
        run_id = uuid.UUID(str(handle.run_id))
        session.commit()

        # The lake is the input: every text-bearing signal in the window, and its values
        # normalized per source so an upvote and a pageview can share one scale.
        points = load_attention_points(
            session, as_of=as_of, window_days=window_days, source_ids=source_ids
        )
        mining = mine(
            ((point.text, point.ts, point.source_id) for point in points),
            taxonomy=taxonomy,
            as_of=as_of,
            window_days=window_days,
            prune_fraction=prune_fraction,
            min_keep=min_keep,
            max_age_days=max_age_days,
        )
        scoring = score_phrases(mining.phrases, points, taxonomy=taxonomy, as_of=as_of,
                                weights=weights)

        written = 0
        existing = 0
        if not dry_run and scoring.candidates:
            ids = upsert_candidates(session, mining.phrases)
            session.flush()
            records = [
                ScoreRecord(
                    candidate_id=ids[candidate_key(candidate.phrase, candidate.category_id)],
                    result=candidate.result,
                    fad=candidate.fad,
                    revenue=candidate.revenue,
                )
                for candidate in scoring.candidates
                if candidate_key(candidate.phrase, candidate.category_id) in ids
            ]
            written = write_snapshots(session, run_id, records)
            existing = len(records) - written
            session.commit()

        digest = "" if dry_run else snapshot_hash(session, run_id)
        status = "ok" if scoring.scored else "empty"
        ledger_note = (
            f"{mining.summary()} | {scoring.summary()} | weights {weights.version}"
            + (" | DRY RUN: nothing written" if dry_run else "")
        )
        if dry_run:
            # A dry run still creates its run row (start_run mints the id the report quotes), so it
            # must also *close* it. Leaving it open made health report a live run for a night that
            # had ended, and left a row a future resume could mistake for real work.
            # `aborted` rather than `ok`: nothing was written, and readers that select finished work
            # (the ranked table, the viewer's default run) must not pick a rehearsal as a result.
            finish_run(
                session,
                str(run_id),
                status="aborted",
                layer_status={
                    "L1": {"status": "ok", **mining.as_dict()},
                    "L3": {"status": status, **scoring.as_dict()},
                },
                notes=f"{ledger_note} | DRY RUN closed as aborted: no scores, no snapshots",
            )
            session.commit()
        else:
            finish_run(
                session,
                str(run_id),
                status=status,
                layer_status={
                    "L1": {"status": "ok", **mining.as_dict()},
                    "L3": {"status": status, **scoring.as_dict()},
                },
                notes=ledger_note,
            )
            session.commit()

        ranked = tuple(
            sorted(
                scoring.candidates,
                key=lambda candidate: (-candidate.mgs, candidate.phrase),
            )
        )
        return DecideReport(
            run_id=run_id,
            status=status,
            mining=mining,
            scoring=scoring,
            snapshots_written=written,
            snapshots_existing=existing,
            snapshot_hash=digest,
            top=ranked[:top],
            notes=(ledger_note,),
        )


def read_ranked(session: Session, *, limit: int = 25) -> list[RankedScore]:
    """The ranked table from the database, for `--rank`."""
    return latest_ranked(session, limit=limit)
