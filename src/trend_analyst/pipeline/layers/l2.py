"""L2 — enrichment: pay for the scarce sources, only where a verdict says it is worth it.

Spec §2 puts L2 between mining and deciding: *"L2 Enrich (Tier-A sources, top-K only)"*. The build
brief's P4 adds *"L2 only, on-demand, top-K"*. This module is that layer, and three rules make it
safe to run nightly:

* **Only what the Judge asked for.** The candidate list is the Judge's `kept` verdicts, ordered by
  score, cut to ``top_k``. The Judge's `enrich` list (its own words: "ebay sold listings") is passed
  to the plugin as the phrase to look up, so the scarcest budget follows a decision rather than a
  guess.
* **The leash is the ledger.** Every request goes through the source's `SourceBudget`, and every
  spend is written to `quota_ledger` under the (run, source, operation) compare-and-swap key — so a
  resumed night cannot pay twice for the same enrichment.
* **No credential is a degradation, not a zero.** A Tier-A plugin without keys raises, and the layer
  records "skipped: no credential" with the reason. It never reports zero listings, because absence
  of data and absence of demand are different facts.

The plugins themselves stay pure: this layer owns the budget, the ledger, the lake write and the
signals, exactly as L0 does — the difference is which candidates it chooses and why.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from trend_analyst.pipeline.orchestrator import _store_raw, _store_signals
from trend_analyst.pipeline.state import record_spend, spent_today
from trend_analyst.sources.base import (
    FetchContext,
    RawBatch,
    SourceBudget,
    SourcePlugin,
    SourceSkippedError,
)
from trend_analyst.store.models import Candidate, Judgement, Score

__all__ = ["DEFAULT_L2_SOURCES", "EnrichmentOutcome", "L2Report", "enrich_candidates"]

#: Tier-A sources L2 knows how to drive, in execution order. The registry still decides what is
#: enabled; this list only says which plugins this phase implements.
DEFAULT_L2_SOURCES: Final[tuple[str, ...]] = ("ebay_browse", "bestbuy_trending")


@dataclass(slots=True)
class EnrichmentOutcome:
    """One candidate's enrichment by one source."""

    phrase: str
    source_id: str
    status: str
    items: int = 0
    signals: int = 0
    quota_spent: int = 0
    reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "source_id": self.source_id,
            "status": self.status,
            "items": self.items,
            "signals": self.signals,
            "quota_spent": self.quota_spent,
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class L2Report:
    """What enrichment did: which candidates, which sources, what it cost, what it refused."""

    status: str = "ok"
    reason: str = ""
    candidates: int = 0
    outcomes: list[EnrichmentOutcome] = field(default_factory=list)
    skipped_sources: dict[str, str] = field(default_factory=dict)

    @property
    def enriched(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == "ok")

    @property
    def signals_written(self) -> int:
        return sum(outcome.signals for outcome in self.outcomes)

    @property
    def quota_spent(self) -> int:
        return sum(outcome.quota_spent for outcome in self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "candidates": self.candidates,
            "enriched": self.enriched,
            "signals": self.signals_written,
            "quota_spent": self.quota_spent,
            "skipped_sources": dict(self.skipped_sources),
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
        }

    def summary(self) -> str:
        if self.status != "ok":
            return f"L2 enrich: {self.status} — {self.reason}"
        skipped = (
            "; skipped " + ", ".join(f"{key} ({value})" for key, value in
            self.skipped_sources.items())
            if self.skipped_sources
            else ""
        )
        return (
            f"L2 enrich: {self.enriched} candidate(s) enriched from {self.candidates} kept, "
            f"{self.signals_written} signal(s), {self.quota_spent} request(s){skipped}"
        )


def enrich_candidates(
    session: Session,
    *,
    run_id: Any,
    plugins: dict[str, SourcePlugin],
    source_ids: Sequence[str] = DEFAULT_L2_SOURCES,
    top_k: int = 5,
    budgets: dict[str, SourceBudget] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    client_for: Callable[[str], Any] | None = None,
    clock: Any = None,
    context_factory: Callable[..., FetchContext] | None = None,
) -> L2Report:
    """Enrich the top-K kept candidates from the enabled Tier-A sources.

    Args:
        plugins: the Tier-A plugins, keyed by source id. Injected so tests drive stubs and the
            nightly run drives the real ones.
        budgets: one `SourceBudget` per source; a missing entry means "no leash", which the layer
            refuses rather than trusting.
        context_factory: builds a `FetchContext`; injected for offline replays and tests.
    """
    report = L2Report()

    queued = _top_kept(session, top_k=top_k)
    report.candidates = len(queued)
    if not queued:
        report.status = "empty"
        report.reason = "no kept candidate needs enrichment"
        return report

    for source_id in source_ids:
        plugin = plugins.get(source_id)
        if plugin is None:
            report.skipped_sources[source_id] = "no plugin in this build"
            continue
        budget = (budgets or {}).get(source_id)
        if budget is None:
            report.skipped_sources[source_id] = "no budget configured: the leash is required"
            continue
        already_spent = spent_today(session, source_id, now=now)
        if already_spent >= budget.budget_per_day:
            report.skipped_sources[source_id] = (
                f"daily budget exhausted "
                f"({already_spent}/{budget.budget_per_day})"
            )
            continue

        for candidate in queued:
            phrase = _enrichment_query(session, candidate)
            if not phrase:
                continue
            outcome = _enrich_one(
                session,
                run_id=run_id,
                plugin=plugin,
                source_id=source_id,
                candidate=candidate,
                phrase=phrase,
                budget=budget,
                now=now,
                dry_run=dry_run,
                client_for=client_for,
                clock=clock,
                context_factory=context_factory,
            )
            report.outcomes.append(outcome)
            if outcome.status == "ok" and not dry_run:
                session.flush()
    return report


def _top_kept(session: Session, *, top_k: int) -> list[Candidate]:
    """The candidates worth paying for: `kept` by the Judge, best score first."""
    newest = (
        select(Score.candidate_id, Score.mgs, Score.scored_at)
        .order_by(Score.scored_at.desc())
        .subquery()
    )
    statement = (
        select(Candidate)
        .join(newest, newest.c.candidate_id == Candidate.id)
        .where(Candidate.status == "kept")
        .order_by(newest.c.mgs.desc(), Candidate.phrase.asc())
        .limit(top_k)
    )
    return list(session.execute(statement).scalars().all())


def _enrichment_query(session: Session, candidate: Candidate) -> str:
    """What to ask the Tier-A source for.

    The Judge's `enrich` list is where the gate says what it wants ("ebay sold listings"); this
    layer
    turns that into the phrase the API needs. The candidate's own words are always part of it, and
    the gate's hints are appended, because "ebay sold listings" alone would search for eBay.
    """
    hints: list[str] = []
    judgement = session.execute(
        select(Judgement)
        .where(Judgement.candidate_id == int(candidate.id))
        .where(Judgement.gate == "judge")
        .order_by(Judgement.created_at.desc(), Judgement.id.desc())
        .limit(1)
    ).scalars().first()
    if judgement is not None:
        hints = [str(item) for item in (judgement.enrich or [])]
    phrase = str(candidate.phrase).strip()
    if not hints:
        return phrase
    # Hints describe the *kind* of data wanted, not the product: searching "circ saw ebay sold
    # listings" would match listings about listings. So the phrase is what gets asked, and the hints
    # stay recorded on the judgement for the reader. If a future source needs an argument built from
    # them, it should be that source's own parser of them — not a string concatenation here.
    del hints
    return phrase


def _enrich_one(
    session: Session,
    *,
    run_id: Any,
    plugin: SourcePlugin,
    source_id: str,
    candidate: Candidate,
    phrase: str,
    budget: SourceBudget,
    now: datetime | None,
    dry_run: bool,
    client_for: Callable[[str], Any] | None,
    clock: Any,
    context_factory: Callable[..., FetchContext] | None,
) -> EnrichmentOutcome:
    """Fetch, parse, store and account for one candidate's enrichment."""
    outcome = EnrichmentOutcome(phrase=phrase, source_id=source_id, status="ok")

    if context_factory is None or client_for is None or clock is None:
        outcome.status = "skipped"
        outcome.reason = "no transport configured for L2"
        return outcome

    operation = f"l2:{source_id}:{phrase[:80]}"
    # Compare-and-swap first: a resumed night re-running this candidate must not pay twice, and the
    # ledger row is the record of having paid at all.
    if not dry_run and not record_spend(
        session, source_id=source_id, run_id=run_id, operation=operation, amount=1
    ):
        outcome.status = "skipped"
        outcome.reason = "already enriched for this run (ledger compare-and-swap)"
        return outcome

    context = context_factory(
        run_id=str(run_id),
        source_id=source_id,
        client=client_for(source_id),
        budget=budget,
        clock=clock,
        cursor=phrase,
    )
    try:
        batch: RawBatch = plugin.fetch(context)
    except SourceSkippedError as exc:
        outcome.status = "skipped"
        outcome.reason = str(exc)
        return outcome
    except Exception as exc:
        # A Tier-A failure is a gap in tonight's evidence, not a crash: the next candidate and the
        # next source still run.
        outcome.status = "failed"
        outcome.reason = f"{type(exc).__name__}: {exc}"[:300]
        return outcome

    outcome.items = batch.item_count
    signals = plugin.parse(batch)
    outcome.quota_spent = 0 if dry_run else batch.request_count
    if dry_run:
        # `signals` counts what was WRITTEN, so a dry run reports zero and says why. Counting parsed
        # signals here made a dry run look like it had stored rows — the same class of mistake as
        # the P1 `dry_run` bug that wrote the lake.
        outcome.signals = 0
        outcome.reason = f"dry run: would store {len(signals)} signal(s)"
    else:
        outcome.signals = len(signals)
        _store_raw(session, run_id=str(run_id), batch=batch)
        _store_signals(session, signals)
    if signals:
        # The price spread travels with the outcome so the brief and the report can show observed
        # supply and prices without re-reading the lake.
        metadata = dict(signals[0].metadata or {})
        outcome.metadata = metadata
    if not signals:
        outcome.status = "empty"
        outcome.reason = "the source answered but reported no listings for this phrase"
    return outcome
