"""The Writer gate: one page per kept candidate, written from the evidence and fully cited.

Spec §6.2 gives the Writer *"One-page brief: verdict, players, risks, angles, revenue reasoning"* at
a budget of ~30 calls, **top-K only**. §6.3 applies the same grounding rule as the Judge: a factual
claim without quote plus URL never reaches a brief.

Three decisions, each one a way this could quietly produce fiction:

* **Only `kept` candidates are written.** The Judge exists to say what deserves a page; writing
  about everything would make the Judge decorative and the nightly token bill large.
* **Grounding runs against the evidence the brief is *about*, plus the quotes the Judge already
  survived with** — a writer may reuse a Judged citation, but it may not invent a new source. The
  union is deliberate: the Judge's own verdict is evidence the writer is entitled to cite.
* **A brief whose citations all vanish is still stored**, marked ungrounded, and rendered with
  *no surviving citation* at the top. That is the honest outcome: the prose exists, its authority
  does not, and P5's eval rubric is what scores it.

The gate is idempotent per (run, candidate) — the `briefs` unique key does the work, so a resumed
run cannot double-write a page — and it is **free of the network**: the transport is injected,
exactly like the Judge's.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_analyst.llm.gates import candidate_evidence, evidence_urls
from trend_analyst.llm.gateway import (
    DEFAULT_BUDGETS,
    GateBudget,
    GatewayOutcome,
    ProviderSpec,
    Sender,
    call_gate,
    default_chain,
)
from trend_analyst.llm.schemas import WriterBrief
from trend_analyst.pipeline.briefs import RenderedBrief, render_brief
from trend_analyst.store.models import Brief, Candidate, Judgement, Score

__all__ = ["DEFAULT_TOP_K", "WriterReport", "pending_briefs", "write_briefs"]

#: Spec §6.2: the Writer runs on the top-K candidates only.
DEFAULT_TOP_K: Final = 5

WRITER_INSTRUCTIONS: Final = """You are the Writer gate of a product-gap discovery pipeline.
You receive ONE candidate: its phrase, category, computed sub-scores, Monte Carlo revenue estimate,
the evidence the pipeline collected (with URLs), and any quotes a previous gate already verified.

Write a one-page brief for a solo operator deciding whether to build this product:
- "verdict": 3-6 sentences. Is the gap real, and why now?
- "players": existing products or incumbents visible in the evidence.
- "risks": what could make this fail (saturation, seasonality, shipping, regulation).
- "angles": concrete product angles someone could ship.
- "revenue_reasoning": how the demand figures and price band support the estimate, in 2-4 sentences.
- "quotes": up to three quotes COPIED VERBATIM from the evidence or the verified quotes given,
  each with its exact URL.

A quote without both text and its exact URL from the material you were given is discarded before
the brief is stored. Answer with JSON only, matching the schema exactly."""


@dataclass(slots=True)
class WriterReport:
    """What the Writer gate produced: pages written, citations kept, spend, and why it stopped."""

    run_id: str
    status: str = "ok"
    reason: str = ""
    considered: int = 0
    written: int = 0
    skipped_existing: int = 0
    ungrounded: int = 0
    removed_quotes: int = 0
    providers: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    briefs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "empty"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "reason": self.reason,
            "considered": self.considered,
            "written": self.written,
            "skipped_existing": self.skipped_existing,
            "ungrounded": self.ungrounded,
            "removed_quotes": self.removed_quotes,
            "providers": self.providers,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "briefs": self.briefs,
        }

    def summary(self) -> str:
        if not self.ok:
            return f"L3 writer: {self.status} — {self.reason}"
        providers = ", ".join(self.providers) or "no provider"
        return (
            f"L3 writer: {self.written} brief(s) from {self.considered} kept candidate(s) "
            f"({self.ungrounded} ungrounded, {self.removed_quotes} quotes stripped, "
            f"{self.skipped_existing} already written) via {providers}; "
            f"{self.prompt_tokens}+{self.completion_tokens} tokens"
        )


def pending_briefs(
    session: Session,
    *,
    run_id: Any = None,
    top_k: int = DEFAULT_TOP_K,
    candidate_ids: Sequence[int] | None = None,
) -> list[Candidate]:
    """The candidates that deserve a page: judged `kept`, scored, and not yet briefed in this run.

    Ordered by the newest MGS so the top-K is the best K: the Writer's budget is the scarcest in the
    system (spec §6.2's ~30 calls), and spending it on the wrong five candidates is invisible.
    """
    already = select(Brief.candidate_id)
    if run_id is not None:
        already = already.where(Brief.run_id == run_id)

    newest_score = (
        select(Score.candidate_id, Score.mgs, Score.scored_at)
        .order_by(Score.scored_at.desc())
        .subquery()
    )
    statement = (
        select(Candidate, newest_score.c.mgs)
        .join(newest_score, newest_score.c.candidate_id == Candidate.id)
        # ONLY `kept` — a Judge verdict, not merely a status the Judge may overwrite. The first
        # version reused the Judge's OVERWRITABLE set, which includes `dropped`, so the Writer
        # spent its scarcest budget writing pages about candidates the Judge had just rejected.
        .where(Candidate.status == "kept")
        .where(Candidate.id.not_in(already))
        .order_by(newest_score.c.mgs.desc(), Candidate.phrase.asc())
        .limit(top_k)
    )
    if candidate_ids is not None:
        statement = statement.where(Candidate.id.in_(list(candidate_ids)))
    return [row[0] for row in session.execute(statement).all()]


def _latest_score(session: Session, candidate_id: int) -> Score | None:
    return session.execute(
        select(Score)
        .where(Score.candidate_id == candidate_id)
        .order_by(Score.scored_at.desc(), Score.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _judged_quotes(session: Session, candidate_id: int) -> list[dict[str, Any]]:
    """Citations the Judge already verified — evidence the writer is entitled to reuse."""
    row = session.execute(
        select(Judgement)
        .where(Judgement.candidate_id == candidate_id)
        .order_by(Judgement.created_at.desc(), Judgement.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None or not row.quotes:
        return []
    return [dict(quote) for quote in row.quotes if isinstance(quote, dict)]


def _payload_for(session: Session, candidate: Candidate) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The writer's input for one candidate, and the URLs its citations may use."""
    score = _latest_score(session, int(candidate.id))
    evidence = candidate_evidence(session, phrases=[str(candidate.phrase)], per_candidate=5).get(
        str(candidate.phrase), []
    )
    judged = _judged_quotes(session, int(candidate.id))
    payload: dict[str, Any] = {
        "phrase": str(candidate.phrase),
        "category": str(candidate.category),
        "mentions": int(candidate.mentions),
        "evidence": evidence,
        "verified_quotes": judged,
    }
    if score is not None:
        payload["score"] = {
            "mgs": round(float(score.mgs), 2),
            "sub_scores": {
                "dv": round(float(score.demand_velocity), 2),
                "ss": round(float(score.saturation), 2),
                "sp": round(float(score.buyer_pain), 2),
                "mp": round(float(score.money), 2),
                "fe": round(float(score.feasibility), 2),
            },
            "fad_label": str(score.fad_label),
            "fad_probability": round(float(score.fad_probability), 4),
            "revenue": {
                "p10": round(float(score.revenue_p10), 2),
                "p50": round(float(score.revenue_p50), 2),
                "p90": round(float(score.revenue_p90), 2),
            },
            "weights_version": str(score.weights_version),
        }
    allowed = evidence_urls(evidence) + tuple(
        str(quote["url"]) for quote in judged if quote.get("url")
    )
    return payload, allowed


def _budget_wall(
    report: WriterReport, *, limits: GateBudget, calls_spent: int, tokens_spent: int
) -> str:
    """Why the Writer should stop, or "" to carry on. Caps are terminal (spec §6.3)."""
    if calls_spent + report.written + report.skipped_existing >= limits.calls_per_day:
        return (
            f"daily call cap reached after {report.written} brief(s) "
            f"({limits.calls_per_day}/day); the rest stay unbriefed rather than half-written"
        )
    spent = tokens_spent + report.prompt_tokens + report.completion_tokens
    if spent >= limits.tokens_per_day:
        return (
            f"daily token cap reached after {report.written} brief(s) "
            f"({spent}/{limits.tokens_per_day}); the rest stay unbriefed"
        )
    return ""


def write_briefs(
    session: Session,
    *,
    run_id: Any,
    sender: Sender,
    candidates: Sequence[Candidate] | None = None,
    chain: Sequence[ProviderSpec] | None = None,
    budget: GateBudget | None = None,
    top_k: int = DEFAULT_TOP_K,
    calls_spent: int = 0,
    tokens_spent: int = 0,
    now: datetime | None = None,
    dry_run: bool = False,
    bypass_cache: bool = False,
    gateway: Callable[..., GatewayOutcome] | None = None,
) -> WriterReport:
    """Write briefs for the top-K kept candidates, one provider call each.

    One call per candidate rather than a batch: a brief is a page of prose about one product, and
    batching would trade the Writer's quality for a call count the budget does not need here
    (spec §6.2 allows ~30 calls for at most a handful of briefs).
    """
    report = WriterReport(run_id=str(run_id))
    limits = budget or DEFAULT_BUDGETS["writer"]
    providers = tuple(chain) if chain is not None else default_chain()
    run_gate = gateway or call_gate
    stamp = now or datetime.now(UTC)

    queue = (
        list(candidates)
        if candidates is not None
        else pending_briefs(session, run_id=run_id, top_k=top_k)
    )
    report.considered = len(queue)
    if not queue:
        report.status = "empty"
        report.reason = "no kept candidate needs a brief in this run"
        return report

    for candidate in queue:
        wall = _budget_wall(
            report, limits=limits, calls_spent=calls_spent, tokens_spent=tokens_spent
        )
        if wall:
            report.status = "partial"
            report.reason = wall
            break

        payload, allowed_urls = _payload_for(session, candidate)
        step = _write_one(
            session,
            run_id=run_id,
            candidate=candidate,
            payload=payload,
            allowed_urls=allowed_urls,
            limits=limits,
            calls_spent=calls_spent,
            tokens_spent=tokens_spent,
            report=report,
            run_gate=run_gate,
            sender=sender,
            providers=providers,
            stamp=stamp,
            now=now,
            dry_run=dry_run,
            bypass_cache=bypass_cache,
        )
        if step.stop:
            report.status = step.status
            report.reason = step.reason
            break
        if step.skipped:
            report.skipped_existing += 1
        elif step.written:
            report.written += 1
        if step.entry is not None:
            report.briefs.append(step.entry)
            if not step.entry["grounded"]:
                report.ungrounded += 1

        if not dry_run:
            session.flush()

        # Same rule as the Judge: one call can cross the cap, and crossing it must be reported
        # rather than hidden behind an `ok`.
        spent_wall = _budget_wall(
            report, limits=limits, calls_spent=calls_spent, tokens_spent=tokens_spent
        )
        if spent_wall:
            report.status = "partial"
            report.reason = spent_wall
            break

    if report.status == "ok" and report.written == 0 and report.skipped_existing == 0:
        report.status = "empty"
        report.reason = report.reason or "no brief was written"
    return report


@dataclass(slots=True)
class _WriteStep:
    """What one candidate's brief did."""

    written: bool = False
    skipped: bool = False
    stop: bool = False
    status: str = "ok"
    reason: str = ""
    entry: dict[str, Any] | None = None


def _write_one(
    session: Session,
    *,
    run_id: Any,
    candidate: Candidate,
    payload: Mapping[str, Any],
    allowed_urls: Sequence[str],
    limits: GateBudget,
    calls_spent: int,
    tokens_spent: int,
    report: WriterReport,
    run_gate: Any,
    sender: Sender,
    providers: Sequence[ProviderSpec],
    stamp: datetime,
    now: datetime | None,
    dry_run: bool,
    bypass_cache: bool,
) -> _WriteStep:
    """Ask the Writer about one candidate and store the page.

    Extracted so `write_briefs` reads as "walk the queue under budget" and this reads as "what one
    call means".
    """
    prompt = (
        WRITER_INSTRUCTIONS
        + "\n\nCandidate (JSON):\n"
        + json.dumps(payload, sort_keys=True, ensure_ascii=False)
    )
    outcome = run_gate(
        session,
        gate="writer",
        prompt=prompt,
        schema=WriterBrief,
        sender=sender,
        chain=providers,
        budget=limits,
        payload=payload,
        cached_calls_today=calls_spent + report.written,
        cached_tokens_today=tokens_spent + report.prompt_tokens + report.completion_tokens,
        evidence_urls=tuple(allowed_urls),
        run_id=run_id,
        now=now,
        dry_run=dry_run,
        bypass_cache=bypass_cache,
    )
    report.prompt_tokens += outcome.prompt_tokens
    report.completion_tokens += outcome.completion_tokens
    report.removed_quotes += outcome.removed_quotes
    if outcome.provider and outcome.provider not in report.providers:
        report.providers.append(outcome.provider)

    if outcome.dry_run:
        # Nothing was called, so there is nothing to write: a dry run is a stop with a reason, not a
        # degradation. Reporting it as `degraded` said "the provider failed" about a run that made
        # no request at all.
        return _WriteStep(stop=True, status="dry-run",
                          reason="dry run: the gate was not called")

    if outcome.capped:
        return _WriteStep(stop=True, status="partial",
                          reason=outcome.reason or "the gate was capped before this brief")
    if not outcome.ok:
        # Same rule as the Judge: a degraded gate changes nothing. A missing brief is a gap in the
        # report; a half-written one is a lie in the archive.
        return _WriteStep(stop=True, status="degraded",
                          reason=outcome.reason or "gateway returned no brief")

    handled, skipped, entry, failure = _finish_one(
        session,
        run_id=run_id,
        candidate=candidate,
        outcome=outcome,
        stamp=stamp,
        dry_run=dry_run,
    )
    if failure:
        return _WriteStep(stop=True, status="degraded", reason=failure)
    return _WriteStep(written=handled, skipped=skipped, entry=entry)


def _finish_one(
    session: Session,
    *,
    run_id: Any,
    candidate: Candidate,
    outcome: GatewayOutcome,
    stamp: datetime,
    dry_run: bool,
) -> tuple[bool, bool, dict[str, Any] | None, str]:
    """Render and store one brief. Returns (written, skipped_existing, report entry, failure).

    Separated from the loop so the gate's control flow stays readable: the loop decides *whether* to
    keep going, this decides what one candidate's outcome means.
    """
    brief = outcome.value
    if not isinstance(brief, WriterBrief):
        return False, False, None, "gateway returned something that is not a WriterBrief"

    score = _latest_score(session, int(candidate.id))
    if score is None:
        return (
            False,
            False,
            None,
            f"candidate {candidate.phrase!r} has no score snapshot, so a brief would have "
            "nothing to attach to",
        )

    rendered = render_brief(
        brief,
        category=str(candidate.category),
        mgs=float(score.mgs),
        revenue={
            "p10": float(score.revenue_p10),
            "p50": float(score.revenue_p50),
            "p90": float(score.revenue_p90),
        },
        weights_version=str(score.weights_version),
        model=f"{outcome.provider}:{outcome.model}",
        as_of=stamp,
    )
    written = False
    if not dry_run:
        written = _store_brief(
            session,
            run_id=run_id,
            candidate=candidate,
            score=score,
            brief=brief,
            rendered=rendered,
            outcome=outcome,
            now=stamp,
        )
    entry = {
        "phrase": str(candidate.phrase),
        "category": str(candidate.category),
        "mgs": round(float(score.mgs), 2),
        "revenue_p50": round(float(score.revenue_p50), 2),
        "citations": rendered.citations,
        "grounded": rendered.grounded,
        "model": f"{outcome.provider}:{outcome.model}",
        "written": written,
    }
    return written, not written, entry, ""


def _store_brief(
    session: Session,
    *,
    run_id: Any,
    candidate: Candidate,
    score: Score,
    brief: WriterBrief,
    rendered: RenderedBrief,
    outcome: GatewayOutcome,
    now: datetime,
) -> bool:
    """Append the brief and mark the candidate briefed. Returns True when it was new.

    The unique key on (run, candidate) makes a resume a no-op; the candidate's status moves to
    `briefed` only for a row that was actually inserted, so a re-run cannot claim a page it did not
    write.
    """
    statement = (
        pg_insert(Brief)
        .values(
            score_id=int(score.id),
            candidate_id=int(candidate.id),
            run_id=run_id,
            created_at=now,
            verdict=brief.verdict or "",
            body_md=rendered.markdown,
            citations=[quote.model_dump(mode="json") for quote in brief.quotes],
            model=f"{outcome.provider}:{outcome.model}",
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
        )
        .on_conflict_do_nothing(constraint="uq_briefs_run_id")
        .returning(Brief.id)
    )
    inserted = session.execute(statement).scalar_one_or_none()
    if inserted is None:
        return False
    session.execute(
        update(Candidate).where(Candidate.id == candidate.id).values(status="briefed")
    )
    return True


def stored_briefs(session: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    """The newest brief per candidate, for the CLI report and the viewer."""
    rows = session.execute(
        select(Brief, Candidate)
        .join(Candidate, Candidate.id == Brief.candidate_id)
        .order_by(Brief.created_at.desc(), Brief.id.desc())
    ).all()
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for brief, candidate in rows:
        candidate_id = int(candidate.id)
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        citations = brief.citations or []
        out.append(
            {
                "phrase": str(candidate.phrase),
                "category": str(candidate.category),
                "model": str(brief.model),
                "citations": len(citations),
                "prompt_tokens": int(brief.prompt_tokens),
                "completion_tokens": int(brief.completion_tokens),
                "created_at": brief.created_at.isoformat() if brief.created_at else None,
                "verdict": str(brief.verdict)[:200],
                "body_md": str(brief.body_md),
            }
        )
        if len(out) >= limit:
            break
    return out


def brief_markdown(session: Session, phrase: str) -> str | None:
    """One candidate's newest brief body, for `--brief <phrase>`."""
    for row in stored_briefs(session, limit=200):
        if row["phrase"].lower() == phrase.lower():
            return str(row["body_md"])
    return None
