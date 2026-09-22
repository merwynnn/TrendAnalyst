"""The decide run: L1 extraction then L3 scoring, in one single-shot pass.

Lives beside `orchestrator.py` rather than inside it because the two runs have different
shapes: L0's unit of work is a *source*, while L1+L3's unit of work is the whole lake
window. A candidate already scored in this run with this weights version is not scored
again — the snapshot's unique key enforces that in the database rather than in a flag.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from config.categories import Taxonomy, default_taxonomy
from trend_analyst.llm.extract import (
    DEFAULT_CHUNK_SIZE,
    ChunkText,
    build_chunks,
    extract_products,
    heuristic_sender,
)
from trend_analyst.llm.gateway import GateBudget, ProviderSpec, Sender
from trend_analyst.pipeline.layers.l1 import (
    DEFAULT_CHUNK_EXCLUDED_SOURCES,
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MIN_KEEP,
    DEFAULT_PRUNE_FRACTION,
    MIN_DOCUMENTS_FOR_NEWS,
    MiningReport,
    rank_products,
)
from trend_analyst.pipeline.layers.l3 import (
    L3Report,
    ScoredCandidate,
    load_attention_points,
    score_phrases,
)
from trend_analyst.pipeline.runs import close_run, open_run
from trend_analyst.scoring.features import SignalPoint
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
                "chunks": self.mining.chunks,
                "attempted": self.mining.attempted,
                "calls": self.mining.calls,
                "cached": self.mining.cached,
                "failed_chunks": self.mining.failed_chunks,
                "unknown_refs": self.mining.unknown_refs,
                "empty_chunks": self.mining.empty_chunks,
                "stale": self.mining.stale,
                "mined": self.mining.mined,
                "pruned": self.mining.pruned,
                "kept": self.mining.kept,
                "prune_fraction": self.mining.prune_fraction,
                "min_keep": self.mining.min_keep,
                "single_document": self.mining.single_document,
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
        return "(no candidates survived extraction)"

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
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    excluded_sources: frozenset[str] = DEFAULT_CHUNK_EXCLUDED_SOURCES,
    prune_fraction: float = DEFAULT_PRUNE_FRACTION,
    min_keep: int = DEFAULT_MIN_KEEP,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    source_ids: list[str] | None = None,
    sender: Sender | None = None,
    chain: Sequence[ProviderSpec] | None = None,
    budget: GateBudget | None = None,
    extractor_calls_spent: int = 0,
    extractor_tokens_spent: int = 0,
    bypass_cache: bool = False,
    dry_run: bool = False,
    top: int = 10,
) -> DecideReport:
    """Extract products from the lake window, score what survives, append the snapshots.

    Args:
        as_of: the reference instant for every window. Passed in so a replay over the same
            lake reproduces the same snapshots (spec §5.4) — the CLI pins it to the newest
            signal when running offline against fixtures.
        sender: the provider transport for the Extractor gate. None means the gate cannot
            run (offline without a stand-in, or no key): extraction is skipped with a reason
            and there is nothing to score. Tests pass a stub; `--offline` passes the
            deterministic heuristic sender.
        min_keep: the ranking floor; the run's note records it next to the prune fraction
            so a reader can see the prune was a decision, not an accident.
        dry_run: do everything and write nothing — including the candidates, so a dry run
            leaves the database exactly as it found it.
    """
    taxonomy = taxonomy or default_taxonomy()

    with sessions() as session:
        run_id = open_run(session, trigger=trigger)
        session.commit()

        # The lake is the input: every text-bearing signal in the window, and its values
        # normalized per source so an upvote and a pageview can share one scale.
        points = load_attention_points(
            session, as_of=as_of, window_days=window_days, source_ids=source_ids
        )
        minable = [
            (point.text, point.ts, point.source_id)
            for point in points
            if point.source_id not in excluded_sources
        ]
        chunks, index = build_chunks(
            [(text, ts, source_id) for text, ts, source_id in minable],
            chunk_size=chunk_size,
        )
        points_by_id = {global_id: points[position] for global_id, position in index.items()}
        mining = MiningReport(
            texts_scanned=len(minable),
            chunks=len(chunks),
            prune_fraction=prune_fraction,
            min_keep=min_keep,
        )

        scoring, mining_note, status = _extract_and_rank(
            session,
            points=points,
            points_by_id=points_by_id,
            chunks=chunks,
            mining=mining,
            taxonomy=taxonomy,
            as_of=as_of,
            weights=weights,
            window_days=window_days,
            prune_fraction=prune_fraction,
            min_keep=min_keep,
            max_age_days=max_age_days,
            sender=sender,
            chain=chain,
            budget=budget,
            extractor_calls_spent=extractor_calls_spent,
            extractor_tokens_spent=extractor_tokens_spent,
            bypass_cache=bypass_cache,
            dry_run=dry_run,
        )
        ledger_note = (
            f"{mining_note} | {scoring.summary()} | weights {weights.version}"
            + (" | DRY RUN: nothing written" if dry_run else "")
        )

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
            # A dry run still creates its run row (open_run mints the id the report quotes),
            # so it must also *close* it. `aborted` rather than `ok`: nothing was written,
            # and readers that select finished work (the ranked table) must not pick a
            # rehearsal as a result.
            close_run(
                session,
                run_id,
                status="aborted",
                layer_status={
                    "L1": {"status": "ok", **mining.as_dict()},
                    "L3": {"status": status, **scoring.as_dict()},
                },
                notes=f"{ledger_note} | DRY RUN closed as aborted: no scores, no snapshots",
            )
            session.commit()
        else:
            close_run(
                session,
                run_id,
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


def _extract_and_rank(
    session: Session,
    *,
    points: Sequence[SignalPoint],
    points_by_id: dict[int, SignalPoint],
    chunks: Sequence[Sequence[ChunkText]],
    mining: MiningReport,
    taxonomy: Taxonomy,
    as_of: datetime,
    weights: MgSWeights,
    window_days: int,
    prune_fraction: float,
    min_keep: int,
    max_age_days: int,
    sender: Sender | None,
    chain: Sequence[ProviderSpec] | None,
    budget: GateBudget | None,
    extractor_calls_spent: int,
    extractor_tokens_spent: int,
    bypass_cache: bool,
    dry_run: bool,
) -> tuple[L3Report, str, str]:
    """Run the Extractor gate, rank what it found, score the survivors.

    Returns (scoring report, mining note, status). Without a sender — and outside a dry
    run — there is nothing to score: extraction is skipped with a reason, loudly.
    """
    if sender is None and not dry_run:
        note = f"{mining.summary()} | no gate transport available, so nothing was extracted"
        return L3Report(), note, "empty"
    # Without a real sender only dry runs arrive here, and the stand-in is free and
    # deterministic — so a dry run still resolves, ranks and scores, while a dry run
    # with a real sender calls nobody. The stand-in's answers are never cached: a live
    # run must never read them as model output.
    heuristic = sender is None or bool(getattr(sender, "is_heuristic", False))
    extraction = extract_products(
        session,
        chunks,
        sender=sender if sender is not None else heuristic_sender(),
        chain=chain,
        budget=budget,
        calls_spent=extractor_calls_spent,
        tokens_spent=extractor_tokens_spent,
        now=as_of,
        dry_run=dry_run and not heuristic,
        bypass_cache=bypass_cache,
        write_cache=not heuristic and not dry_run,
    )
    mining.chunks = extraction.chunks
    mining.attempted = extraction.attempted
    mining.calls = extraction.calls
    mining.cached = extraction.cached
    mining.failed_chunks = extraction.failed_chunks
    mining.unknown_refs = extraction.unknown_refs
    mining.empty_chunks = extraction.empty_chunks
    if extraction.status == "dry-run":
        return L3Report(), f"{mining.summary()} | DRY RUN: nothing written", "empty"
    ranked = rank_products(
        [(item.phrase, item.category, item.point_ids) for item in extraction.products],
        points_by_id,
        as_of=as_of,
        window_days=window_days,
        prune_fraction=prune_fraction,
        min_keep=min_keep,
        max_age_days=max_age_days,
    )
    mining.mined = ranked.mined
    mining.pruned = ranked.mined - len(ranked.kept)
    mining.kept = len(ranked.kept)
    mining.stale = ranked.stale
    mining.single_document = sum(
        1 for item in ranked.kept if item.documents < MIN_DOCUMENTS_FOR_NEWS
    )
    mining.by_category = {
        item.category_id: sum(
            1 for other in ranked.kept if other.category_id == item.category_id
        )
        for item in ranked.kept
    }
    mining.phrases = ranked.kept
    scoring = score_phrases(
        ranked.kept,
        points,
        taxonomy=taxonomy,
        as_of=as_of,
        weights=weights,
        match_points=ranked.match_points,
    )
    if extraction.status in {"partial", "degraded"}:
        note = f"{mining.summary()} | {extraction.reason}"
    else:
        note = mining.summary()
    if heuristic:
        note += " | heuristic extraction stand-in (offline plumbing, not a model judgement)"
    return scoring, note, ("ok" if scoring.scored else "empty")


def read_ranked(session: Session, *, limit: int = 25) -> list[RankedScore]:
    """The ranked table from the database, for `--rank`."""
    return latest_ranked(session, limit=limit)
