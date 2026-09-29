"""The Extractor gate: lake texts in, grounded product candidates out.

Deterministic mining (n-grams + stop lists + taxonomy) produced fragments more often than
products — measured, not assumed: two-thirds of the Judge's drops were sentence fragments,
and the taxonomy silently deleted the best find until a human noticed. A word list cannot
keep up with language, so extraction is now a gate: the model reads the texts and returns
products, and code enforces everything else.

Three rules keep the gate honest, and each mirrors a Judge-gate lesson:

* **Refs are grounded in code, not in the prompt.** Every product must cite the chunk-local
  ids of the texts that mention it. A ref the chunk did not contain is dropped with a
  count — an invented ref is the extraction equivalent of an invented citation, and the
  "unknown_phrase" rule is what the Judge does about those.
* **A failed chunk is a gap, not a crash.** Whatever one chunk's failure, the other chunks
  still run; what did not get extracted is reported as a count. There are no re-ask rounds:
  unlike a verdict (which a later round can still apply), a failed chunk would cost a full
  second call for the same texts.
* **The cache is the determinism.** The cache key is the normalized chunk, so replaying the
  same lake reproduces the same products — the snapshot-hash check in CI holds. Genuinely
  new lake content may extract differently run to run; that is stated, not hidden.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy.orm import Session

from config.categories import canonical_phrase, normalize_phrase
from trend_analyst.llm.gateway import (
    DEFAULT_BUDGETS,
    GateBudget,
    GatewayOutcome,
    ProviderSpec,
    Sender,
    call_gate,
    default_extractor_chain,
)
from trend_analyst.llm.schemas import ExtractorOutput

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "EXTRACTOR_CATEGORIES",
    "EXTRACTOR_INSTRUCTIONS",
    "MAX_PRODUCTS_PER_CHUNK",
    "TEXT_TRUNCATE_CHARS",
    "ChunkText",
    "ExtractReport",
    "ResolvedProduct",
    "build_chunks",
    "chunk_payload",
    "chunk_prompt",
    "extract_products",
    "extractor_instructions",
    "heuristic_sender",
    "resolve_output",
    "union_products",
]

#: The categories the prompt offers, round-robin assigned by `heuristic_sender` so offline
#: runs spread across buckets the way a night of real answers would.
EXTRACTOR_CATEGORIES: Final[tuple[str, ...]] = (
    "tools_diy",
    "home_office",
    "kitchen_dining",
    "home_improvement",
    "outdoor_garden",
    "pets",
    "fitness_recovery",
    "electronics_accessories",
    "baby_kids",
    "music_audio",
    "beauty_personal_care",
    "fashion_apparel",
    "toys_games",
    "health_wellness",
)

#: Texts per provider call. A hundred texts at ~150 tokens each is ~15k tokens in —
#: one slow round trip instead of three, which is what makes a grown lake affordable.
#: The per-call token count grows, but the night's total barely moves (each text is
#: still read once), while wall-clock time falls with the call count.
DEFAULT_CHUNK_SIZE: Final = 100

#: Products per chunk, bounded so one chatty chunk cannot eat the night's budget.
#: Enforced by the prompt (the model sees the bound), not by truncating its answer.
MAX_PRODUCTS_PER_CHUNK: Final = 40

#: Characters of each text the model sees. Full texts travel with the resolved refs
#: downstream — this only trims what the prompt carries. Product talk declares itself
#: early (titles, opening complaints); the tail is rarely where the product is.
TEXT_TRUNCATE_CHARS: Final = 500

#: Consecutive full-chain chunk failures that stop the run early. One failed chunk is
#: ~27 doomed calls the budget wall never sees (it counts successes), so five in a
#: row with no success between them is proof the chain is down, not a blip — a blip
#: would not survive 3 transient retries on each of 9 providers. Remaining chunks stay
#: unattempted (honest partial, retried next run) rather than failed.
_FAIL_STREAK_STOP: Final = 5

#: Template, not the prompt: {max_products} is filled per call, so the doubled
#: braces are literal JSON braces, not format fields.
_EXTRACTOR_TEMPLATE: Final = """You are the Extractor gate of a product-gap discovery pipeline.
Below are numbered texts people wrote (post titles and quotes, with their source and date).
Return the distinct physical products, digital goods or micro-SaaS ideas they talk about wanting,
complaining about, comparing or asking for.

For EACH product: name it as a clear generic product idea a shopper would search for, in
2-6 plain words USING WORDS FROM THE TEXTS (e.g. "no-drill removable shelf", not "shelf
thing"; "circ saw blade guard", not "cutting tool accessory"). The name must say what the
product IS, never whose it is: no brand names, no model names, no proper nouns ("dyson
vacuum", "V15", "Cat Jarman" are all wrong — "cordless stick vacuum" is right). File it
under exactly one of these categories: baby_kids, beauty_personal_care,
electronics_accessories, fashion_apparel, fitness_recovery, health_wellness,
home_improvement, home_office, kitchen_dining, music_audio, outdoor_garden, pets,
tools_diy, toys_games; and list in doc_ids EVERY numbered text that mentions it (all of
them, not just the first).

Skip, without mentioning: sentence fragments ("filament thanks", "anybody saw"); bare opinions
with no problem, wish or comparison ("excellent filament"); thread furniture ("weekly thread",
"price check"); proper nouns that are not products ("Cat Jarman"); non-English texts unless
they clearly name a product in English words. Overlapping descriptions of the same thing are
ONE product with all of their doc_ids. Return at most {max_products} products: the most
clearly product-shaped ones first.

Answer with JSON only:
{{"products": [{{"phrase": str, "category": str, "doc_ids": [int], "reason": str}}]}}"""

#: Backwards-compatible alias: the instructions with the default product bound. Tests
#: and prompts that do not care about the bound keep reading this name.
EXTRACTOR_INSTRUCTIONS: Final = _EXTRACTOR_TEMPLATE.format(
    max_products=MAX_PRODUCTS_PER_CHUNK
)


def extractor_instructions(*, max_products: int = MAX_PRODUCTS_PER_CHUNK) -> str:
    """The prompt instructions with an explicit product bound."""
    return _EXTRACTOR_TEMPLATE.format(max_products=max_products)


@dataclass(frozen=True, slots=True)
class ChunkText:
    """One minable text, with the id the prompt shows for it (global across the night)."""

    id: int
    source_id: str
    ts: datetime
    text: str


@dataclass(frozen=True, slots=True)
class ResolvedProduct:
    """An extracted product whose refs survived grounding: ids into the night's texts."""

    phrase: str
    category: str
    point_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class ExtractReport:
    """What extraction did: how many chunks, calls, products — and what was refused."""

    status: str = "ok"
    reason: str = ""
    chunks: int = 0
    calls: int = 0
    cached: int = 0
    failed_chunks: int = 0
    empty_chunks: int = 0
    products_raw: int = 0
    unknown_refs: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    products: tuple[ResolvedProduct, ...] = ()
    #: Chunks actually attempted. The invariant below exists because a run once ended
    #: `ok` with fewer attempts than chunks and no reason — silent evidence loss, the
    #: worst outcome this gate can produce. Whatever the cause, it is now loud.
    attempted: int = 0
    #: Why chunks failed, reason -> count. A night that loses 46 of 48 chunks to 429s
    #: once reported only "46 failed" — the count without the cause, which is how a
    #: quota outage reads as a pipeline bug. Truncated: counting, not archiving.
    fail_reasons: dict[str, int] = field(default_factory=dict)
    #: Consecutive full-chain failures. The budget wall counts successful calls, so a
    #: dead chain burns ~27 doomed calls per chunk with no backstop — this streak is
    #: the backstop: at the threshold the run stops dispatching and says why.
    consecutive_failures: int = 0

    @property
    def kept(self) -> int:
        return len(self.products)

    @property
    def top_fail_reason(self) -> str:
        """The most common chunk failure reason, "" when nothing failed."""
        if not self.fail_reasons:
            return ""
        return max(self.fail_reasons.items(), key=lambda item: (item[1], item[0]))[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "chunks": self.chunks,
            "attempted": self.attempted,
            "calls": self.calls,
            "cached": self.cached,
            "failed_chunks": self.failed_chunks,
            "top_fail_reason": self.top_fail_reason,
            "empty_chunks": self.empty_chunks,
            "products_raw": self.products_raw,
            "products_kept": self.kept,
            "unknown_refs": self.unknown_refs,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }

    def summary(self) -> str:
        coverage = (
            f"{self.attempted}/{self.chunks} chunks attempted"
            if self.attempted != self.chunks
            else f"{self.chunks} chunk(s)"
        )
        failed = f"{self.failed_chunks} failed"
        if self.failed_chunks and self.top_fail_reason:
            failed += f" (top reason: {self.top_fail_reason[:160]})"
        return (
            f"L1 extract: {self.kept} product(s) from {coverage} "
            f"({self.calls} call(s), {self.cached} cached, {failed}, "
            f"{self.unknown_refs} invented ref(s) dropped)"
        )


def build_chunks(
    texts: Sequence[tuple[str, datetime, str]], *, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> tuple[list[tuple[ChunkText, ...]], dict[int, int]]:
    """Pack ``(text, timestamp, source_id)`` triples into numbered chunks.

    Sorted by (timestamp, source, text) first, so the chunks — and therefore the cache
    keys and the prompts — are identical for identical lake content no matter what order
    the query returned. Ids are global across the night (0..N-1), so a ref identifies one
    text wherever unioning happens later.

    Returns the chunks and the id -> input-position index, so the caller can map a
    resolved ref back to whatever row the input came from.
    """
    enumerated = [(index, text, ts, source_id) for index, (text, ts, source_id) in enumerate(texts)]
    ordered = sorted(
        ((str(text), ts, str(source_id), position)
         for position, text, ts, source_id in enumerated if str(text)),
        key=lambda item: (item[1], item[2], item[0]),
    )
    numbered = [
        ChunkText(id=index, source_id=source_id, ts=ts, text=text)
        for index, (text, ts, source_id, _position) in enumerate(ordered)
    ]
    index = {
        chunk_text.id: ordered[chunk_text.id][3] for chunk_text in numbered
    }
    chunks = [
        tuple(numbered[start : start + chunk_size])
        for start in range(0, len(numbered), chunk_size)
    ]
    return chunks, index


def _shown_text(text: str) -> str:
    """What the model sees of one text: whitespace-collapsed, truncated.

    Truncation and the cache key agree by construction (both call this), so a cached
    answer always matches the prompt it was computed from.
    """
    collapsed = " ".join(text.split())
    return collapsed[:TEXT_TRUNCATE_CHARS]


def chunk_prompt(
    chunk: Sequence[ChunkText], *, max_products: int = MAX_PRODUCTS_PER_CHUNK
) -> str:
    """The prompt for one chunk: instructions, then the numbered texts."""
    lines = [extractor_instructions(max_products=max_products), ""]
    for item in chunk:
        stamp = item.ts.date().isoformat() if isinstance(item.ts, datetime) else str(item.ts)
        lines.append(f"[{item.id}] ({item.source_id}, {stamp}) {_shown_text(item.text)}")
    return "\n".join(lines)


def chunk_payload(chunk: Sequence[ChunkText]) -> dict[str, Any]:
    """The cache-key material for one chunk: the texts, without prompt decoration.

    Two prompts that differ only in whitespace or wrapping share a cache entry, because
    what matters is the work (these texts), not the envelope. Truncated exactly like
    the prompt, so the key always describes what the model actually saw.
    """
    return {
        "texts": [
            {
                "id": item.id,
                "source": item.source_id,
                "ts": item.ts.isoformat() if isinstance(item.ts, datetime) else str(item.ts),
                "text": _shown_text(item.text),
            }
            for item in chunk
        ]
    }


def resolve_output(
    output: ExtractorOutput, *, valid_ids: set[int]
) -> tuple[list[ResolvedProduct], int]:
    """Ground one answer: keep products with surviving refs, count invented ones.

    A product with no surviving ref is not a product with weak evidence — it is a claim
    about texts that do not exist, and it is dropped whole. Duplicate phrases inside one
    answer merge their refs rather than splitting the evidence across two rows.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    invented = 0
    for product in output.products:
        # Canonical at birth: one run's "LED Strips" and the next night's "led strip"
        # become the same stored phrase, so the candidates table dedupes across runs
        # instead of accumulating spelling variants.
        phrase = canonical_phrase(product.phrase)
        if not phrase:
            continue
        kept = {ref for ref in product.doc_ids if ref in valid_ids}
        invented += len(product.doc_ids) - len(kept)
        if not kept:
            continue
        # The first-seen phrasing wins: merging is by normalized key, display is human.
        key = (str(product.category), normalize_phrase(phrase))
        entry = merged.setdefault(key, {"phrase": phrase, "ids": set[int]()})
        entry["ids"].update(kept)
    resolved = [
        ResolvedProduct(
            phrase=str(entry["phrase"]), category=category,
            point_ids=tuple(sorted(entry["ids"])),
        )
        for (category, _normalized), entry in merged.items()
    ]
    return resolved, invented


def union_products(
    batches: Sequence[Sequence[ResolvedProduct]],
) -> list[ResolvedProduct]:
    """Union products across chunks, merging refs for the same normalized product.

    Chunks are extracted independently, so the same product surfaces in several of them.
    Merging (rather than competing) is what keeps a widely-mentioned product's full
    mention series — the velocity scorer reads the union, not the fragments.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for batch in batches:
        for product in batch:
            key = (product.category, normalize_phrase(product.phrase))
            entry = merged.setdefault(
                key, {"phrase": product.phrase, "ids": set[int]()}
            )
            entry["ids"].update(product.point_ids)
    return [
        ResolvedProduct(
            phrase=str(entry["phrase"]), category=category, point_ids=tuple(sorted(entry["ids"]))
        )
        for (category, _normalized), entry in merged.items()
    ]


def heuristic_sender() -> Sender:
    """Offline plumbing stand-in: leading words as phrases, valid refs, no model.

    Each chunk answers with up to three products built from its own texts (the first five
    words of every tenth text),     categories round-robined, every ref real. Deterministic
    by construction, so `--offline` replays and the gate exercise resolve → rank → score
    without a provider and without spend. Never quality: the phrases are sentence
    prefixes, and the report of an offline run says so where a reader will meet it.

    Marked with `is_heuristic` so callers can tell it apart from a provider transport:
    a dry run with the stand-in still resolves, ranks and scores (nothing to spend),
    while a dry run with a real sender calls nobody.
    """

    def send(_provider: ProviderSpec, prompt: str) -> tuple[str, int, int]:
        ids: list[int] = []
        for line in prompt.splitlines():
            if line.startswith("[") and "]" in line:
                try:
                    ids.append(int(line[1 : line.index("]")]))
                except ValueError:
                    continue
        products = [
            {
                "phrase": f"offline product {doc_id}",
                "category": EXTRACTOR_CATEGORIES[index % len(EXTRACTOR_CATEGORIES)],
                "doc_ids": [doc_id],
                "reason": "offline stand-in, not a model judgement",
            }
            for index, doc_id in enumerate(sorted(ids)[::10][:3])
        ]
        return json.dumps({"products": products}), 50, 10

    # setattr, not assignment: mypy rejects the attribute on a Sender, ruff would
    # prefer the assignment — the noqa sides with mypy, and the marker is the
    # documented way callers tell the stand-in apart from a transport.
    setattr(send, "is_heuristic", True)  # noqa: B010
    return send


def _budget_wall(
    report: ExtractReport,
    *,
    limits: GateBudget,
    calls_spent: int,
    tokens_spent: int,
) -> str:
    """Why the gate should stop, or "" to carry on. Caps are terminal (spec §6.3)."""
    if calls_spent + report.calls >= limits.calls_per_day:
        return (
            f"daily call cap reached after {report.calls} call(s) "
            f"({limits.calls_per_day}/day); remaining chunks stay unextracted rather "
            "than being silently dropped"
        )
    spent = tokens_spent + report.prompt_tokens + report.completion_tokens
    if spent >= limits.tokens_per_day:
        return (
            f"daily token cap reached after {report.calls} call(s) "
            f"({spent}/{limits.tokens_per_day} tokens); remaining chunks stay unextracted"
        )
    return ""


def extract_products(
    session: Session,
    chunks: Sequence[Sequence[ChunkText]],
    *,
    sender: Sender,
    chain: Sequence[ProviderSpec] | None = None,
    budget: GateBudget | None = None,
    calls_spent: int = 0,
    tokens_spent: int = 0,
    now: datetime | None = None,
    dry_run: bool = False,
    bypass_cache: bool = False,
    write_cache: bool = True,
    run_gate: Callable[..., GatewayOutcome] | None = None,
    progress: Callable[[str], None] | None = None,
    dispatch_pause_s: float = 0.0,
) -> ExtractReport:
    """Extract products chunk by chunk, grounding every ref in code.

    Strictly sequential, one chunk after the next, on the caller's session: parallel
    calls once bought latency and paid for it in burst bans, idle-transaction
    timeouts and half the complexity in this file. Sequential with a dispatch pause
    stays under per-minute limits by construction.

    Args:
        sender: the provider transport (injected; tests pass a stub, production the real one).
        calls_spent, tokens_spent: today's spend for the gate, read once by the caller.
        dry_run: count the chunks, call nobody, resolve nothing.
        write_cache: skip cache writes (the heuristic stand-in always passes False: its
            answers must never be cached as model output).
        progress: called with one line per chunk (index, provider, cached/live, yield).
        dispatch_pause_s: seconds between chunk dispatches (free-tier limits are
            per-minute: the 5s default stays near 12/min, under flash-lite's 15 RPM).
    """
    report = ExtractReport(chunks=len(chunks))
    if not chunks:
        report.status = "empty"
        report.reason = "no texts to extract from"
        return report
    if dry_run:
        report.status = "dry-run"
        report.reason = "dry run: the provider was not called"
        return report

    limits = budget or DEFAULT_BUDGETS["extractor"]
    providers = tuple(chain) if chain is not None else default_extractor_chain()
    gate = run_gate or call_gate
    total = len(chunks)
    resolved_batches: list[list[ResolvedProduct]] = []

    for position, chunk in enumerate(chunks, start=1):
        if position > 1 and dispatch_pause_s > 0:
            time.sleep(dispatch_pause_s)
        if _apply_chunk(
            report, chunk, resolved_batches, position=position, total=total,
            gate=gate, session=session, sender=sender, providers=providers,
            limits=limits, calls_spent=calls_spent, tokens_spent=tokens_spent,
            now=now, bypass_cache=bypass_cache, write_cache=write_cache,
            progress=progress,
        ) == "stop":
            break

    report.products = tuple(union_products(resolved_batches))
    if report.status == "ok" and report.attempted < len(chunks):
        report.status = "partial"
        report.reason = (
            f"loop ended after {report.attempted} of {len(chunks)} chunks with no "
            "reason recorded; treating the rest as unprocessed rather than done"
        )
    if report.status == "ok" and report.failed_chunks and not report.products:
        report.status = "degraded"
        report.reason = f"{report.failed_chunks} chunk(s) failed and none produced products"
    return report


def _apply_chunk(
    report: ExtractReport,
    chunk: Sequence[ChunkText],
    resolved_batches: list[list[ResolvedProduct]],
    *,
    position: int,
    total: int,
    gate: Callable[..., GatewayOutcome],
    session: Session,
    sender: Sender,
    providers: tuple[ProviderSpec, ...],
    limits: GateBudget,
    calls_spent: int,
    tokens_spent: int,
    now: datetime | None,
    bypass_cache: bool,
    write_cache: bool,
    progress: Callable[[str], None] | None,
) -> str:
    """Run one chunk through the wall, the gate and grounding. Returns stop/continue."""
    wall = _budget_wall(report, limits=limits, calls_spent=calls_spent,
                        tokens_spent=tokens_spent)
    if wall:
        report.status = "partial"
        report.reason = wall
        return "stop"
    report.attempted += 1
    outcome = gate(
        session,
        gate="extractor",
        prompt=chunk_prompt(chunk),
        schema=ExtractorOutput,
        sender=sender,
        chain=providers,
        budget=limits,
        payload=chunk_payload(chunk),
        cached_calls_today=calls_spent + report.calls,
        cached_tokens_today=tokens_spent + report.prompt_tokens + report.completion_tokens,
        run_id=None,
        now=now,
        bypass_cache=bypass_cache,
        write_cache=write_cache,
    )
    return _record_outcome(
        report, chunk, resolved_batches, outcome,
        position=position, total=total, progress=progress,
    )


def _streak_stop_reason(streak: int, top_reason: str) -> str:
    """Why the run stopped dispatching: the chain is down, not flaky."""
    reason = (
        f"stopped after {streak} consecutive chunk failures with no success between "
        "them: the provider chain is down, not flaky"
    )
    if top_reason:
        reason += f" (top: {top_reason[:160]})"
    return reason + "; remaining chunks unprocessed rather than failed"


def _note_chunk_failure(report: ExtractReport, reason: str) -> str:
    """Count one failed chunk; trip the breaker on a streak. Returns stop/continue."""
    report.failed_chunks += 1
    key = (reason or "unknown").strip()[:160] or "unknown"
    report.fail_reasons[key] = report.fail_reasons.get(key, 0) + 1
    report.consecutive_failures += 1
    if report.consecutive_failures >= _FAIL_STREAK_STOP:
        report.status = "partial"
        report.reason = _streak_stop_reason(
            report.consecutive_failures, report.top_fail_reason
        )
        return "stop"
    return "continue"


def _record_outcome(
    report: ExtractReport,
    chunk: Sequence[ChunkText],
    resolved_batches: list[list[ResolvedProduct]],
    outcome: GatewayOutcome,
    *,
    position: int,
    total: int,
    progress: Callable[[str], None] | None,
) -> str:
    """Fold one chunk's outcome into the report. Returns stop/continue."""
    if outcome.capped:
        report.status = "partial"
        report.reason = outcome.reason
        return "stop"
    if not outcome.ok or not isinstance(outcome.value, ExtractorOutput):
        if progress is not None:
            progress(f"L1 extract [{position}/{total}] failed: {outcome.reason[:120]}")
        return _note_chunk_failure(report, outcome.reason)
    report.consecutive_failures = 0
    report.calls += 1
    if outcome.cached:
        report.cached += 1
    report.prompt_tokens += outcome.prompt_tokens
    report.completion_tokens += outcome.completion_tokens
    report.products_raw += len(outcome.value.products)
    valid = {item.id for item in chunk}
    resolved, invented = resolve_output(outcome.value, valid_ids=valid)
    report.unknown_refs += invented
    if not resolved:
        report.empty_chunks += 1
    resolved_batches.append(resolved)
    if progress is not None:
        served = "cached" if outcome.cached else f"{outcome.provider}:{outcome.model}"
        progress(
            f"L1 extract [{position}/{total}] {served} "
            f"({outcome.prompt_tokens + outcome.completion_tokens} tok) -> "
            f"{len(resolved)} product(s), {invented} invented ref(s) dropped"
        )
    return "continue"
