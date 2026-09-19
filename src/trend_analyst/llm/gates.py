"""The Judge gate: batched keep/drop with cited quotes, applied to the candidate table.

Spec §6.2 gives the Judge one job — *"keep/drop + fad probability + enrich list, JSON, cited
quotes"* — batched nightly (~20 calls), and §6.3 makes grounding a code-enforced rule. This module
is that gate, and it is built so the *offline* half is complete: the live provider call is one
injected function, so batching, grounding, verdict application, persistence and degradation are all
testable with no network and no quota.

Four decisions worth stating, because each one is a way this could quietly corrupt the pipeline:

* **A degraded gate changes nothing.** If the budget is spent or every provider fails, candidates
  keep their current status and the reason is recorded. A Judge that "drops everything" because a
  provider was down would silently empty the pipeline, and the next night's run would look clean.
* **A verdict must name a candidate in its own batch.** A model that invents a phrase has its
  verdict counted as `unknown_phrase` and dropped — never applied to a lookalike.
* **Status flips are allowed, mining is not.** The Judge may move a candidate between `kept` and
  `dropped` as verdicts change; it never resurrects a candidate a human pruned, and it never
  touches `briefed`.
* **Grounding is checked against what the pipeline collected.** The URLs a candidate's own signals
  carry are the only ones a quote may cite; anything else is stripped and the verdict is marked
  `ungrounded` rather than discarded.
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

from trend_analyst.llm.cache import cache_key, token_spend_by_gate
from trend_analyst.llm.gateway import (
    DEFAULT_BUDGETS,
    GateBudget,
    GatewayOutcome,
    ProviderSpec,
    Sender,
    call_gate,
    default_chain,
)
from trend_analyst.llm.schemas import JudgeBatch, JudgeVerdict
from trend_analyst.store.models import Candidate, Judgement, Run, Score, SignalRow

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "JudgeReport",
    "JudgedCandidate",
    "candidate_evidence",
    "evidence_urls",
    "judge_candidates",
    "latest_judgements",
    "pending_judgements",
]

#: How many candidates go into one provider call. Spec §6.2 budgets the Judge at ~20 calls a
#: night; a batch of ten keeps a night of twenty candidates to two calls, and keeps each prompt
#: small enough that a free tier can carry it (LESSONS §6.5).
DEFAULT_BATCH_SIZE: Final = 10

#: Statuses a judgement is allowed to overwrite. `briefed` is a human-visible outcome and
#: `pruned` was a decision by mining; a verdict does not get to undo either.
OVERWRITABLE: Final[frozenset[str]] = frozenset({"active", "kept", "dropped"})

JUDGE_INSTRUCTIONS: Final = """You are the Judge gate of a product-gap discovery pipeline.
For each candidate you receive a product phrase, its category, its computed sub-scores, and the
evidence the pipeline collected from named sources (with URLs).

Decide for EACH candidate:
- "keep" if it looks like a real, monetisable product gap worth enriching with market data;
- "drop" if it is noise, a niche too small to matter, or something a solo operator cannot ship.
Also give a fad call ("fad" | "trend" | "evergreen") with a probability, a short reason, up to two
quotes COPIED VERBATIM from that candidate's evidence together with their exact URL, and a list of
what to enrich (e.g. "ebay sold listings", "amazon review volume").

Rules that are enforced in code, so a violation simply loses you the citation:
- a quote without both text and its exact URL from the evidence given is discarded;
- a verdict for a phrase not in this batch is discarded.

Answer with JSON only:
{"verdicts": [{"phrase": str, "category": str, "decision": "keep"|"drop", "confidence": 0..1,
"fad_label": "fad"|"trend"|"evergreen"|null, "fad_probability": 0..1|null, "reason": str,
"quotes": [{"text": str, "url": str, "source_id": str}], "enrich": [str]}]}"""


@dataclass(frozen=True, slots=True)
class JudgedCandidate:
    """One candidate as the Judge sees it: its scores and the evidence behind it."""

    candidate_id: int
    phrase: str
    category: str
    status: str
    mentions: int
    mgs: float
    sub_scores: dict[str, float]
    revenue: dict[str, float]
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(
            str(item["url"]) for item in self.evidence if item.get("url")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "category": self.category,
            "mentions": self.mentions,
            "mgs": round(self.mgs, 2),
            "sub_scores": {key: round(value, 2) for key, value in self.sub_scores.items()},
            "revenue_p50": round(self.revenue.get("p50", 0.0), 2),
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class JudgeReport:
    """What the gate did: verdicts applied, phrases ignored, spend, and why it stopped."""

    run_id: str
    status: str = "ok"
    reason: str = ""
    batches: int = 0
    judged: int = 0
    kept: int = 0
    dropped: int = 0
    unchanged: int = 0
    ungrounded: int = 0
    removed_quotes: int = 0
    unknown_phrase: int = 0
    missing_verdicts: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    per_batch: list[dict[str, Any]] = field(default_factory=list)
    judgements_written: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "reason": self.reason,
            "batches": self.batches,
            "judged": self.judged,
            "kept": self.kept,
            "dropped": self.dropped,
            "unchanged": self.unchanged,
            "ungrounded": self.ungrounded,
            "removed_quotes": self.removed_quotes,
            "unknown_phrase": self.unknown_phrase,
            "missing_verdicts": self.missing_verdicts[:10],
            "providers": self.providers,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "judgements_written": self.judgements_written,
            "per_batch": self.per_batch,
        }

    def summary(self) -> str:
        if not self.ok:
            return f"L3 judge: {self.status} — {self.reason}"
        return (
            f"L3 judge: {self.judged} judged over {self.batches} call(s) "
            f"({self.kept} kept, {self.dropped} dropped, {self.unchanged} unchanged, "
            f"{self.ungrounded} ungrounded, {self.removed_quotes} quotes stripped, "
            f"{self.unknown_phrase} unknown) via {', '.join(self.providers) or 'no provider'}; "
            f"{self.prompt_tokens}+{self.completion_tokens} tokens"
        )


# ---------------------------------------------------------------------------
# Evidence assembly
# ---------------------------------------------------------------------------
def candidate_evidence(
    session: Session,
    *,
    phrases: Sequence[str],
    per_candidate: int = 4,
    max_chars: int = 320,
) -> dict[str, list[dict[str, Any]]]:
    """The collected evidence for each phrase: named source, URL, timestamp and text.

    One query for the whole batch rather than one per candidate, and matched in the same
    normalized space mining uses (the phrase may appear in a body, not only a title — the bug the
    viewer found). The URL is carried because grounding needs it: a quote without a URL the
    pipeline actually stored cannot be cited.
    """
    if not phrases:
        return {}
    wanted = {phrase.lower() for phrase in phrases}
    rows = session.execute(
        select(
            SignalRow.entity,
            SignalRow.quote,
            SignalRow.source_id,
            SignalRow.ts,
            SignalRow.value,
            SignalRow.url,
        ).order_by(SignalRow.ts.desc())
    ).all()

    found: dict[str, list[dict[str, Any]]] = {phrase: [] for phrase in phrases}
    for row in rows:
        haystack = f"{row.entity} {row.quote or ''}".lower()
        for phrase in wanted:
            if phrase not in haystack:
                continue
            for key, bucket in found.items():
                if key.lower() != phrase or len(bucket) >= per_candidate:
                    continue
                bucket.append(
                    {
                        "source": str(row.source_id),
                        "ts": row.ts.isoformat() if row.ts else None,
                        "value": float(row.value),
                        "url": row.url,
                        "text": " ".join(str(row.entity).split())[:max_chars],
                        "quote": " ".join(str(row.quote).split())[:max_chars]
                        if row.quote
                        else None,
                    }
                )
    return dict(found)


def evidence_urls(evidence: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Every URL a candidate's evidence actually carries — the grounding allowlist."""
    return tuple(str(item["url"]) for item in evidence if item.get("url"))


def pending_judgements(
    session: Session,
    *,
    limit: int = DEFAULT_BATCH_SIZE * 3,
    run_id: Any = None,
) -> list[Candidate]:
    """Candidates the Judge has not yet ruled on in this run, best score first.

    Ordered by the newest MGS so a truncated batch judges the most promising candidates first:
    if the budget runs out mid-night, what was skipped is what mattered least.
    """
    judged = select(Judgement.candidate_id).where(Judgement.gate == "judge")
    if run_id is not None:
        judged = judged.where(Judgement.run_id == run_id)
    newest = (
        select(Score.candidate_id, Score.mgs)
        .order_by(Score.scored_at.desc())
        .subquery()
    )
    statement = (
        select(Candidate)
        .outerjoin(newest, newest.c.candidate_id == Candidate.id)
        .where(Candidate.status.in_(sorted(OVERWRITABLE)))
        .where(Candidate.id.not_in(judged))
        .order_by(newest.c.mgs.desc().nullslast(), Candidate.phrase.asc())
        .limit(limit)
    )
    return list(session.execute(statement).scalars().all())


def _score_context(session: Session, candidate_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The newest score per candidate: what the Judge is being asked to judge *of*."""
    if not candidate_ids:
        return {}
    rows = session.execute(
        select(Score).where(Score.candidate_id.in_(list(candidate_ids)))
    ).scalars().all()
    newest: dict[int, Score] = {}
    for row in rows:
        current = newest.get(int(row.candidate_id))
        if current is None or row.scored_at > current.scored_at:
            newest[int(row.candidate_id)] = row
    return {
        candidate_id: {
            "mgs": float(score.mgs),
            "sub_scores": {
                "dv": float(score.demand_velocity),
                "ss": float(score.saturation),
                "sp": float(score.buyer_pain),
                "mp": float(score.money),
                "fe": float(score.feasibility),
            },
            "fad_label": str(score.fad_label),
            "fad_probability": float(score.fad_probability),
            "revenue": {
                "p10": float(score.revenue_p10),
                "p50": float(score.revenue_p50),
                "p90": float(score.revenue_p90),
            },
            "weights_version": str(score.weights_version),
        }
        for candidate_id, score in newest.items()
    }


def build_batch(
    session: Session,
    candidates: Sequence[Candidate],
) -> list[JudgedCandidate]:
    """Assemble the payload for one provider call: candidates, scores and their evidence."""
    scores = _score_context(session, [int(candidate.id) for candidate in candidates])
    evidence = candidate_evidence(
        session, phrases=[str(candidate.phrase) for candidate in candidates]
    )
    batch: list[JudgedCandidate] = []
    for candidate in candidates:
        context = scores.get(int(candidate.id), {})
        batch.append(
            JudgedCandidate(
                candidate_id=int(candidate.id),
                phrase=str(candidate.phrase),
                category=str(candidate.category),
                status=str(candidate.status),
                mentions=int(candidate.mentions),
                mgs=float(context.get("mgs", 0.0)),
                sub_scores=dict(context.get("sub_scores", {})),
                revenue=dict(context.get("revenue", {})),
                evidence=evidence.get(str(candidate.phrase), []),
            )
        )
    return batch


def _runner(
    sender: Sender | None,
    chain: Sequence[ProviderSpec] | None,
) -> Callable[..., GatewayOutcome]:
    """The gateway call, with its arguments bound — one seam for tests to replace."""

    def run(session: Session, **kwargs: Any) -> GatewayOutcome:
        return call_gate(session, **kwargs)

    return run


def _budget_wall(
    report: JudgeReport,
    *,
    limits: GateBudget,
    calls_spent: int,
    tokens_spent: int,
) -> str:
    """Why the gate should stop, or "" to carry on. Caps are terminal (spec §6.3)."""
    if calls_spent + report.batches >= limits.calls_per_day:
        return (
            f"daily call cap reached after {report.batches} batch(es) "
            f"({limits.calls_per_day}/day); remaining candidates stay unjudged rather than "
            "being silently dropped"
        )
    spent = tokens_spent + report.prompt_tokens + report.completion_tokens
    if spent >= limits.tokens_per_day:
        return (
            f"daily token cap reached after {report.batches} batch(es) "
            f"({spent}/{limits.tokens_per_day} tokens); remaining candidates stay unjudged"
        )
    return ""


def _apply_batch(
    session: Session,
    *,
    run_id: Any,
    batch: Sequence[JudgedCandidate],
    verdicts: Sequence[JudgeVerdict],
    outcome: GatewayOutcome,
    payload: Mapping[str, Any],
    report: JudgeReport,
    dry_run: bool,
) -> None:
    """Apply one batch's verdicts to the candidates and to the report's counters."""
    by_phrase = {item.phrase.lower(): (index, item) for index, item in enumerate(batch)}
    applied = 0
    for verdict in verdicts:
        found = by_phrase.get(verdict.phrase.lower())
        if found is None:
            # A model that invents a phrase does not get to change a lookalike.
            report.unknown_phrase += 1
            continue
        index, target = found
        if not dry_run:
            _apply_verdict(
                session,
                run_id=run_id,
                target=target,
                verdict=verdict,
                outcome=outcome,
                key=cache_key("judge", outcome.model, payload),
            )
        applied += 1
        report.judged += 1
        if verdict.ungrounded:
            report.ungrounded += 1
        if verdict.decision == "keep":
            report.kept += 1
        else:
            report.dropped += 1
        if batch[index].status == verdict.decision:
            report.unchanged += 1

    answered = {verdict.phrase.lower() for verdict in verdicts}
    report.missing_verdicts.extend(
        item.phrase for item in batch if item.phrase.lower() not in answered
    )
    report.per_batch.append(
        {
            "batch": report.batches,
            "status": outcome.status,
            "applied": applied,
            "missing": len(batch) - applied,
            "provider": outcome.provider,
            "model": outcome.model,
            "tokens": outcome.prompt_tokens + outcome.completion_tokens,
        }
    )


def judge_candidates(
    session: Session,
    *,
    run_id: Any,
    sender: Sender,
    candidates: Sequence[Candidate] | None = None,
    chain: Sequence[ProviderSpec] | None = None,
    budget: GateBudget | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    calls_spent: int = 0,
    tokens_spent: int = 0,
    now: datetime | None = None,
    dry_run: bool = False,
    gateway: Callable[..., GatewayOutcome] | None = None,
    limit: int = DEFAULT_BATCH_SIZE * 3,
    bypass_cache: bool = False,
) -> JudgeReport:
    """Judge the candidates a decide run produced, and apply the verdicts.

    Args:
        sender: the provider transport (injected; tests pass a stub, production the real one).
        candidates: the batch to judge; defaults to the unjudged candidates for this run.
        calls_spent, tokens_spent: today's spend for the gate, read once by the caller so a night
            of batches shares one read instead of one per batch.
        dry_run: assemble and call nothing, write nothing.
    """
    report = JudgeReport(run_id=str(run_id))
    limits = budget or DEFAULT_BUDGETS["judge"]
    providers = tuple(chain) if chain is not None else default_chain()
    run_gate = gateway or _runner(sender, providers)

    queue = list(candidates) if candidates is not None else pending_judgements(
        session, limit=limit, run_id=run_id
    )
    if not queue:
        report.status = "empty"
        report.reason = "no unjudged candidates for this run"
        return report

    for start in range(0, len(queue), batch_size):
        chunk = queue[start : start + batch_size]
        wall = _budget_wall(
            report, limits=limits, calls_spent=calls_spent, tokens_spent=tokens_spent
        )
        if wall:
            report.status = "partial"
            report.reason = wall
            break

        batch = build_batch(session, chunk)
        payload = {"candidates": [item.as_dict() for item in batch]}
        urls = tuple(url for item in batch for url in item.urls)
        prompt = (
            JUDGE_INSTRUCTIONS
            + "\n\nCandidates (JSON):\n"
            + json.dumps(payload, sort_keys=True, ensure_ascii=False)
        )

        outcome = run_gate(
            session,
            gate="judge",
            prompt=prompt,
            schema=JudgeBatch,
            sender=sender,
            chain=providers,
            budget=limits,
            payload=payload,
            cached_calls_today=calls_spent + report.batches,
            cached_tokens_today=tokens_spent + report.prompt_tokens + report.completion_tokens,
            evidence_urls=urls,
            run_id=run_id,
            now=now,
            dry_run=dry_run,
            bypass_cache=bypass_cache,
        )
        report.batches += 1
        report.prompt_tokens += outcome.prompt_tokens
        report.completion_tokens += outcome.completion_tokens
        report.removed_quotes += outcome.removed_quotes
        if outcome.provider and outcome.provider not in report.providers:
            report.providers.append(outcome.provider)

        if not outcome.ok:
            # A degraded batch must not become a mass deletion: candidates keep their status and
            # the reason travels with the report.
            report.status = "degraded"
            report.reason = outcome.reason or "gateway returned no verdicts"
            report.per_batch.append({"batch": report.batches, "status": outcome.status,
                                     "reason": outcome.reason[:200]})
            break

        _apply_batch(
            session,
            run_id=run_id,
            batch=batch,
            verdicts=outcome.value.verdicts if isinstance(outcome.value, JudgeBatch) else [],
            outcome=outcome,
            payload=payload,
            report=report,
            dry_run=dry_run,
        )
        if not dry_run:
            session.flush()

    if report.status == "ok" and report.judged == 0:
        report.status = "empty"
        report.reason = "the gate answered, but with no applicable verdicts"
    return report


def _apply_verdict(
    session: Session,
    *,
    run_id: Any,
    target: JudgedCandidate,
    verdict: JudgeVerdict,
    outcome: GatewayOutcome,
    key: str,
) -> None:
    """Write the judgement and move the candidate's status, in that order.

    The judgement row is the evidence; the status is the index. The unique key on
    (candidate, run, gate) makes both idempotent under a resumed run.
    """
    session.execute(
        pg_insert(Judgement)
        .values(
            candidate_id=target.candidate_id,
            run_id=run_id,
            gate="judge",
            decision=verdict.decision,
            confidence=verdict.confidence,
            fad_label=verdict.fad_label,
            fad_probability=verdict.fad_probability,
            reason=verdict.reason,
            quotes=[quote.model_dump(mode="json") for quote in verdict.quotes],
            enrich=list(verdict.enrich),
            ungrounded=verdict.ungrounded,
            dropped_quotes=verdict.dropped_quotes,
            provider=outcome.provider,
            model=outcome.model,
            cache_key=key,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
        )
        .on_conflict_do_nothing(constraint="uq_judgements_candidate")
    )
    new_status = "kept" if verdict.decision == "keep" else "dropped"
    session.execute(
        update(Candidate)
        .where(Candidate.id == target.candidate_id)
        .where(Candidate.status.in_(sorted(OVERWRITABLE)))
        .values(status=new_status)
    )


def latest_judgements(session: Session, *, limit: int = 200) -> dict[int, Judgement]:
    """The newest judgement per candidate, for the viewer and for reporting."""
    rows = session.execute(
        select(Judgement).order_by(Judgement.created_at.asc(), Judgement.id.asc())
    ).scalars().all()
    newest: dict[int, Judgement] = {}
    for row in rows[: max(limit, 1) * 4]:
        newest[int(row.candidate_id)] = row
    return newest


def judge_runs(session: Session) -> list[Run]:
    """Runs that actually judged something, newest first."""
    return list(
        session.execute(
            select(Run)
            .where(Run.id.in_(select(Judgement.run_id).where(Judgement.gate == "judge").distinct()))
            .order_by(Run.started_at.desc())
        ).scalars().all()
    )


def spend_snapshot(session: Session) -> dict[str, dict[str, int]]:
    """Tokens per gate, straight from the cache rows (the brief's token log)."""
    return token_spend_by_gate(session)


def utcnow() -> datetime:
    return datetime.now(UTC)
