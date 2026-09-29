"""The dashboard: three static HTML pages, rewritten at the end of every run.

Niches first: every scored idea grouped by its category (one category = one
problem-space whose products compete for the same wallet), niches ranked by a niche
score, products ranked by MGS inside. Each niche card carries its AI overview — the
pain gate's problem line + rationale — and every product row expands (<details>) to
its evidence: which sources found it, what they said, the judge's verdict and reason,
the brief when one exists, and why it did or did not advance.

The pages: `index.html` (niches, ranked), `winners.html` (judge-kept products, full
detail — the shortlist for building), `movers.html` (new ideas and score changes
since the previous run — the reason to come back). Linked by a nav row, no server,
no JavaScript: three self-contained files. The nightly run regenerates them (see
`run_nightly`'s tail), and the live workflow publishes them to GitHub Pages. Renders
from the database, never from memory — the pages always show what is actually stored.
"""

from __future__ import annotations

import argparse
import html
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from config.categories import normalize_phrase
from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.llm.gates import latest_judgements
from trend_analyst.llm.writer import stored_briefs
from trend_analyst.scoring.passion import category_passion
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import Run, Score, SignalRow
from trend_analyst.store.snapshots import RankedScore, latest_ranked

__all__ = ["build_dashboard", "main", "niche_score"]

#: The niche score, stated once: pain the model judged (40%), passion the lake measured
#: as engagement depth (30%), momentum as the niche's top-5 mean MGS (30%). Weights are
#: a starting point the dashboard states openly rather than a truth it hides.
PAIN_WEIGHT: Final = 0.4
PASSION_WEIGHT: Final = 0.3
MOMENTUM_WEIGHT: Final = 0.3

#: Cap on ideas and evidence rows: the page stays readable, the counts stay exact.
_IDEAS_LIMIT: Final = 1000
_EVIDENCE_QUOTES: Final = 6
_EVIDENCE_URLS: Final = 8
#: Scoring runs the movers page compares: the latest against the one before it.
_COMPARED_RUNS: Final = 2
#: Pain badge bands, matching the prompt's rubric (0-20 enthusiasm … 81-100 suffering).
_PAIN_HIGH: Final = 51
_PAIN_MID: Final = 21
#: Cut-list rows shown per niche card; the header carries the exact total.
_CUT_LIST_SHOWN: Final = 20


def niche_score(*, pain: float | None, passion: float, top_mgs: Sequence[float]) -> float:
    """One niche's rank: 0.4 pain + 0.3 passion + 0.3 top-5 mean MGS.

    An unassessed niche (no pain score yet) ranks on passion + momentum only, rescaled
    — a missing assessment must never read as "no pain".
    """
    momentum = sum(top_mgs[:5]) / max(len(top_mgs[:5]), 1) if top_mgs else 0.0
    if pain is None:
        known = PASSION_WEIGHT + MOMENTUM_WEIGHT
        return (PASSION_WEIGHT * passion + MOMENTUM_WEIGHT * momentum) / known
    return PAIN_WEIGHT * pain + PASSION_WEIGHT * passion + MOMENTUM_WEIGHT * momentum


def build_dashboard(
    sessions: sessionmaker[Session],
    *,
    out_dir: Path,
    as_of: datetime | None = None,
    window_days: int = 90,
) -> Path:
    """Read the stored results and write the three pages into `out_dir`.

    `index.html` (niches, ranked), `winners.html` (judge-kept products, full detail),
    `movers.html` (new ideas and score changes since the previous scored run).
    Returns the index path.
    """
    with sessions() as session:
        run = _latest_nightly_run(session)
        ideas = latest_ranked(session, limit=_IDEAS_LIMIT)
        judgements = latest_judgements(session, limit=_IDEAS_LIMIT)
        briefs = {
            (row["phrase"], row["category"]): row
            for row in stored_briefs(session, limit=200)
        }
        moment = as_of or _as_of(session)
        passion = category_passion(session, as_of=moment, window_days=window_days)
        evidence = _evidence_lookup(session, [idea.phrase for idea in ideas], moment, window_days)
        previous = _previous_scored(session, ideas)
    bundle = {
        "run": run,
        "ideas": ideas,
        "judgements": judgements,
        "briefs": briefs,
        "passion": passion,
        "evidence": evidence,
        "moment": moment,
        "previous": previous,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(_render_index(bundle), encoding="utf-8")
    (out_dir / "winners.html").write_text(_render_winners(bundle), encoding="utf-8")
    (out_dir / "movers.html").write_text(_render_movers(bundle), encoding="utf-8")
    return out_dir / "index.html"


def _previous_scored(
    session: Session, ideas: Sequence[RankedScore]
) -> dict[str, Any]:
    """The two latest scoring runs, for the movers page.

    Every decide run scores everything mined, so the newest run's snapshots are the
    current picture and the run before it is the baseline: new ideas are scored in
    the current run but not the baseline, movers are scored in both with a different
    MGS. One run so far means every idea is new.
    """
    run_ids = [
        row[0]
        for row in session.execute(
            select(Score.run_id, func.max(Score.scored_at))
            .group_by(Score.run_id)
            .order_by(func.max(Score.scored_at).desc())
            .limit(_COMPARED_RUNS)
        ).all()
    ]
    if not run_ids:
        return {"current": (), "previous": {}}
    current = [idea for idea in ideas if idea.run_id == run_ids[0]] or list(ideas)
    previous: dict[int, RankedScore] = {}
    if len(run_ids) == _COMPARED_RUNS:
        previous = {
            item.candidate_id: item
            for item in latest_ranked(session, run_id=run_ids[1], limit=_IDEAS_LIMIT)
        }
    return {"current": current, "previous": previous}


def _latest_nightly_run(session: Session) -> Run | None:
    """The newest finished nightly run that actually decided something.

    Each stage writes its own run row (L0, decide, L2, gates), so the newest nightly
    row is often the judge/writer one — which carries no L1/pain data. The dashboard
    explains the latest *results*, so it reads the newest row that has them, falling
    back to the newest finished row when no stage has decided yet.
    """
    rows = session.execute(
        select(Run)
        .where(Run.trigger == "nightly", Run.status.in_(["ok", "degraded", "empty"]))
        .order_by(Run.started_at.desc())
        .limit(10)
    ).scalars().all()
    for row in rows:
        if (row.layer_status or {}).get("L1"):
            return row
    return rows[0] if rows else None


def _as_of(session: Session) -> datetime:
    newest = session.execute(
        select(SignalRow.ts).order_by(SignalRow.ts.desc()).limit(1)
    ).scalar()
    if newest is None:
        return datetime.now(UTC)
    return newest if newest.tzinfo else newest.replace(tzinfo=UTC)


def _evidence_lookup(
    session: Session, phrases: Sequence[str], moment: datetime, window_days: int
) -> dict[str, dict[str, Any]]:
    """Which sources found each idea, and what they said — matched from the lake.

    One query for the window, plain-Python matching (the same normalized space the
    scorer matches in): per idea the sources, a few quotes with URLs, and counts.
    """
    cutoff = moment - timedelta(days=window_days)
    rows = session.execute(
        select(
            SignalRow.source_id,
            SignalRow.entity,
            SignalRow.quote,
            SignalRow.url,
            SignalRow.ts,
        ).where(SignalRow.ts >= cutoff)
    ).all()
    normalized = {phrase: normalize_phrase(phrase) for phrase in phrases}
    out: dict[str, dict[str, Any]] = {
        phrase: {"sources": set(), "quotes": [], "urls": [], "mentions": 0}
        for phrase in phrases
    }
    for row in rows:
        haystack = normalize_phrase(f"{row.entity or ''} {row.quote or ''}")
        for phrase, needle in normalized.items():
            if not needle or needle not in haystack:
                continue
            bucket = out[phrase]
            bucket["mentions"] += 1
            bucket["sources"].add(str(row.source_id))
            if row.quote and len(bucket["quotes"]) < _EVIDENCE_QUOTES:
                bucket["quotes"].append(str(row.quote)[:300])
            if row.url and row.url not in bucket["urls"] and len(bucket["urls"]) < _EVIDENCE_URLS:
                bucket["urls"].append(str(row.url))
    return out


_STYLE: Final = """<style>
:root { color-scheme: light; }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 1080px;
  margin: 0 auto; padding: 24px 16px 64px; color: #1a1a1a; background: #fafafa; }
header p { color: #555; }
nav { display: flex; gap: 8px; margin: 12px 0 4px; }
nav a { padding: 6px 14px; border: 1px solid #ddd; border-radius: 999px; color: #0b5bd3;
  text-decoration: none; background: #fff; font-size: .9rem; }
nav a.here { background: #0b5bd3; color: #fff; border-color: #0b5bd3; }
.niche { background: #fff; border: 1px solid #e2e2e2; border-radius: 12px;
  padding: 18px 20px; margin: 18px 0; }
.niche h2 { margin: 0 0 4px; font-size: 1.25rem; }
.scores { display: flex; gap: 16px; flex-wrap: wrap; margin: 8px 0; font-size: .9rem; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-weight: 600; }
.pain-high { background: #fde2e2; color: #8f1d1d; }
.pain-mid { background: #fef3d8; color: #7a5b00; }
.pain-low { background: #e3f2e7; color: #1d5c33; }
.pain-na { background: #eee; color: #555; }
.keep { background: #e3f2e7; color: #1d5c33; }
.drop { background: #fde2e2; color: #8f1d1d; }
.up { color: #1d5c33; font-weight: 600; } .down { color: #8f1d1d; font-weight: 600; }
.problem { font-weight: 600; } .rationale { color: #444; }
table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: .9rem; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; vertical-align: top; }
th { color: #666; font-weight: 600; }
details { margin-top: 4px; } summary { cursor: pointer; color: #0b5bd3; }
.ev { color: #444; font-size: .85rem; } .ev blockquote { margin: 6px 0; padding-left: 10px;
  border-left: 3px solid #ddd; color: #333; }
.bar { height: 8px; border-radius: 4px; background: #eee; min-width: 80px; }
.bar i { display: block; height: 8px; border-radius: 4px; background: #0b5bd3; }
.cut { color: #777; font-size: .85rem; }
footer { color: #777; font-size: .8rem; margin-top: 32px; }
</style>"""


def _nav(active: str) -> str:
    """The three pages, with the current one marked. Relative links: the whole site is
    three files that work from disk or from Pages with no server."""
    links = [("index.html", "niches"), ("winners.html", "winners"), ("movers.html", "movers")]
    items = "".join(
        f'<a href="{href}"' + (' class="here"' if href == active else "") + f">{label}</a>"
        for href, label in links
    )
    return f"<nav>{items}</nav>"


def _shell(
    *, title: str, active: str, heading: str, intro: str, body: str, moment: datetime
) -> str:
    """One page of the three-file site: shared chrome, per-page heading and body."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrendAnalyst — {html.escape(title)}</title>
{_STYLE}
</head>
<body>
<header>
<h1>{html.escape(heading)}</h1>
{_nav(active)}
<p>{intro}</p>
</header>
{body}
<footer>Generated {html.escape(moment.isoformat())} by TrendAnalyst. Pain: LLM-judged.
Passion: engagement depth from collected signals. Nothing here is buying advice.</footer>
</body>
</html>"""


def _niche_list(
    ideas: Sequence[RankedScore],
    pain_niches: Mapping[str, Any],
    passion: Mapping[str, float],
) -> list[dict[str, Any]]:
    """Group ideas into ranked niches, with pain assessments attached. Shared by the
    index (all ideas) and the winners page (pain context per winner)."""
    by_niche: dict[str, list[RankedScore]] = defaultdict(list)
    for idea in ideas:
        by_niche[idea.category].append(idea)
    niches = []
    for category, unsorted in by_niche.items():
        items = sorted(unsorted, key=lambda item: (-item.mgs, item.phrase))
        niches.append(_niche_entry(category, items, pain_niches, passion))
    # Niches the pain gate assessed but that scored nothing this window still get a
    # card: pain without products is a lead, not an absence.
    for category in pain_niches:
        if category not in by_niche:
            niches.append(_niche_entry(category, [], pain_niches, passion))
    niches.sort(key=lambda niche: (-niche["score"], niche["category"]))
    return niches


def _niche_entry(
    category: str,
    items: list[RankedScore],
    pain_niches: Mapping[str, Any],
    passion: Mapping[str, float],
) -> dict[str, Any]:
    pain = pain_niches.get(category) or {}
    pain_value = pain.get("pain_score")
    assessed = pain_value if isinstance(pain_value, (int, float)) else None
    return {
        "category": category,
        "items": items,
        "pain": assessed,
        "problem": str(pain.get("painful_problem") or ""),
        "rationale": str(pain.get("rationale") or ""),
        "passion": float(passion.get(category, 0.0)),
        "score": niche_score(
            pain=assessed,
            passion=float(passion.get(category, 0.0)),
            top_mgs=[item.mgs for item in items],
        ),
    }


def _render_index(bundle: dict[str, Any]) -> str:
    run = bundle["run"]
    ideas = bundle["ideas"]
    moment = bundle["moment"]
    layer = dict(getattr(run, "layer_status", None) or {})
    pain_niches = dict(((layer.get("pain") or {}).get("niches")) or {})
    dropped = list((layer.get("L1") or {}).get("dropped") or [])
    dropped_by_phrase = {item.get("phrase"): item for item in dropped if item.get("phrase")}
    judgements = bundle["judgements"]
    briefs = bundle["briefs"]
    evidence = bundle["evidence"]
    passion = bundle["passion"]

    niches = _niche_list(ideas, pain_niches, passion)
    # Ideas pruned below the velocity shortlist, grouped for their niche's "didn't make
    # the cut" list. Judge-dropped ideas stay in the main table with the judge's reason.
    dropped_by_niche: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in dropped:
        dropped_by_niche[str(item.get("category", "?"))].append(
            {"phrase": str(item.get("phrase", "?")), "reason": str(item.get("reason", ""))}
        )

    cards = "\n".join(
        _niche_card(
            niche=niche,
            judgements=judgements,
            briefs=briefs,
            evidence=evidence,
            dropped=dropped_by_niche.get(niche["category"], []),
            dropped_by_phrase=dropped_by_phrase,
            rank=position,
        )
        for position, niche in enumerate(niches, start=1)
    )
    run_line = _run_line(run, moment, len(ideas), len(niches))
    return _shell(
        title="niches, ranked",
        active="index.html",
        heading="Niches, ranked",
        intro=(
            f"{run_line}<br>Pain is the model's judgement (0&ndash;100), passion is "
            "measured engagement depth, momentum is the niche's top-5 mean MGS. "
            "Niche score = 0.4 pain + 0.3 passion + 0.3 momentum. "
            "Click any idea for its evidence and why it did or did not advance."
        ),
        body=cards or "<p>No scored ideas yet — run the pipeline first.</p>",
        moment=moment,
    )


def _pain_niches(run: Run | None) -> dict[str, Any]:
    """The run's pain assessments by category, {} when the gate never ran."""
    layer = dict(getattr(run, "layer_status", None) or {})
    return dict(((layer.get("pain") or {}).get("niches")) or {})


def _render_winners(bundle: dict[str, Any]) -> str:
    """The winning products: everything the judge kept, richest detail first.

    One card per winner, best MGS first: the score with its sub-scores, the niche's
    pain verdict, the judge's reason with its quotes, the brief when one exists, and
    the lake evidence behind it. Winners are few by design — this page rewards
    reading, not scanning.
    """
    ideas = bundle["ideas"]
    moment = bundle["moment"]
    judgements = bundle["judgements"]
    briefs = bundle["briefs"]
    evidence = bundle["evidence"]
    niche_pain = {
        niche["category"]: niche
        for niche in _niche_list(ideas, _pain_niches(bundle["run"]), bundle["passion"])
    }
    winners = sorted(
        (
            idea
            for idea in ideas
            if (judgements.get(idea.candidate_id) is not None)
            and str(getattr(judgements[idea.candidate_id], "decision", "")) == "keep"
        ),
        key=lambda idea: (-idea.mgs, idea.phrase),
    )
    cards = "\n".join(
        _winner_card(
            idea=idea,
            rank=position,
            judgement=judgements[idea.candidate_id],
            brief=briefs.get((idea.phrase, idea.category)),
            evidence=evidence.get(idea.phrase, {}),
            niche=niche_pain.get(idea.category, {}),
        )
        for position, idea in enumerate(winners, start=1)
    )
    return _shell(
        title="winning products",
        active="winners.html",
        heading="Winners",
        intro=(
            f"{len(winners)} judge-kept product(s). Everything here survived scoring "
            "and a keep verdict — the shortlist for actually building something."
        ),
        body=cards or "<p>No winners yet — nothing has earned a keep verdict.</p>",
        moment=moment,
    )


def _winner_card(
    *,
    idea: RankedScore,
    rank: int,
    judgement: Any,
    brief: Mapping[str, Any] | None,
    evidence: Mapping[str, Any],
    niche: Mapping[str, Any],
) -> str:
    """One winner, fully opened: scores, pain, verdict, brief, evidence."""
    quotes = [
        (str(item.get("text", "")), str(item.get("url", "")))
        for item in (getattr(judgement, "quotes", None) or [])
        if isinstance(item, dict) and item.get("text")
    ]
    quote_blocks = "".join(
        f"<blockquote>{html.escape(text)}"
        + (f'<br><a href="{html.escape(url)}">{html.escape(url[:80])}</a>' if url else "")
        + "</blockquote>"
        for text, url in quotes[:3]
    )
    lake_quotes = "".join(
        f"<blockquote>{html.escape(text)}</blockquote>"
        for text in (evidence.get("quotes", []) if isinstance(evidence, dict) else [])[:4]
    )
    lake_urls = "".join(
        f'<div><a href="{html.escape(url)}">{html.escape(url[:80])}</a></div>'
        for url in (evidence.get("urls", []) if isinstance(evidence, dict) else [])[:6]
    )
    sources = sorted(evidence.get("sources", set())) if isinstance(evidence, dict) else []
    brief_html = (
        f"<p><b>Brief:</b> {html.escape(str(brief.get('verdict', '')))}</p>"
        if brief is not None
        else ""
    )
    pain_line = ""
    if niche.get("pain") is not None:
        pain_line = (
            f"<p><b>Niche pain {niche['pain']:.0f}:</b> "
            f"{html.escape(str(niche.get('problem') or ''))} — "
            f"{html.escape(str(niche.get('rationale') or ''))}</p>"
        )
    interest = "—" if idea.interest is None else f"{idea.interest:.0f}"
    return f"""<section class="niche">
<h2>#{rank} {html.escape(idea.phrase)}</h2>
<div class="scores"><span class="badge keep">judge: keep</span>
{_pain_badge(niche.get("pain"))}
<span>{html.escape(idea.category)} · passion {float(niche.get("passion", 0.0)):.0f}</span>
<span>{html.escape(idea.fad_label)} ({idea.fad_probability:.2f})</span>
<span>$/mo P50 {idea.revenue_p50:,.0f}</span></div>
<div class="bar"><i style="width: {idea.mgs:.0f}%"></i></div>
<p>MGS {idea.mgs:.1f} — DV {idea.demand_velocity:.0f} · SS {idea.saturation:.0f} ·
SP {idea.buyer_pain:.0f} · MP {idea.money:.0f} · FE {idea.feasibility:.0f} ·
CI {interest} [weights {html.escape(idea.weights_version)}];
mentions {idea.mentions}</p>
{pain_line}
<p><b>Judge</b> (conf {float(getattr(judgement, "confidence", 0) or 0):.2f}):
{html.escape(str(getattr(judgement, "reason", "")))}</p>
{quote_blocks}
{brief_html}
<div class="ev"><p><b>What people said:</b></p>{lake_quotes or "<p>—</p>"}
<p><b>Found in:</b> {html.escape(", ".join(sources) or "—")}</p>{lake_urls}</div>
</section>"""


def _render_movers(bundle: dict[str, Any]) -> str:
    """What changed: new ideas and the biggest score moves since the previous run.

    The reason to come back after every run. New ideas are scored in the latest run
    but in none before; risers and fallers are scored in both, ordered by MGS delta.
    """
    moment = bundle["moment"]
    previous = bundle["previous"]
    current = list(previous.get("current", ()))
    baseline = dict(previous.get("previous", {}))
    fresh = [idea for idea in current if idea.candidate_id not in baseline]
    # Deltas only within one weights version: v1 and v2 MGS are different formulas,
    # and diffing them would read as momentum. Cross-version ideas list without a badge.
    moved = [
        (
            None
            if idea.weights_version != baseline[idea.candidate_id].weights_version
            else idea.mgs - baseline[idea.candidate_id].mgs,
            idea,
        )
        for idea in current
        if idea.candidate_id in baseline
    ]
    scored_moves = [(delta, idea) for delta, idea in moved if delta is not None]
    risers = sorted(((d, i) for d, i in scored_moves if d > 0), reverse=True)[:10]
    fallers = sorted(((d, i) for d, i in scored_moves if d < 0))[:10]

    def _row(delta: float | None, idea: RankedScore) -> str:
        cls = "" if delta is None else ("up" if delta > 0 else "down")
        badge = "" if delta is None else f' <span class="{cls}">{delta:+.1f}</span>'
        return (
            f"<tr><td><b>{html.escape(idea.phrase)}</b></td>"
            f"<td>{html.escape(idea.category)}</td>"
            f"<td>{idea.mgs:.1f}{badge}</td>"
            f"<td>{html.escape(idea.fad_label)}</td>"
            f"<td>{idea.revenue_p50:,.0f}</td></tr>"
        )

    fresh_rows = "".join(_row(None, idea) for idea in sorted(fresh, key=lambda i: -i.mgs)[:30])
    riser_rows = "".join(_row(delta, idea) for delta, idea in risers)
    faller_rows = "".join(_row(delta, idea) for delta, idea in fallers)
    if not baseline:
        note = "<p>First scored run — everything below is new.</p>"
    else:
        note = (
            f"<p>Since the previous scored run: {len(fresh)} new, "
            f"{len(risers)} up, {len(fallers)} down.</p>"
        )
    body = (
        f"{note}"
        f"<h2>New ideas ({len(fresh)})</h2>"
        f"<table><tr><th>idea</th><th>niche</th><th>MGS</th><th>fad</th><th>$/mo P50</th></tr>"
        f"{fresh_rows or '<tr><td colspan=5>Nothing new this run.</td></tr>'}</table>"
        f"<h2>Risers ({len(risers)})</h2>"
        f"<table><tr><th>idea</th><th>niche</th><th>MGS ±</th><th>fad</th><th>$/mo P50</th></tr>"
        f"{riser_rows or '<tr><td colspan=5>No risers.</td></tr>'}</table>"
        f"<h2>Fallers ({len(fallers)})</h2>"
        f"<table><tr><th>idea</th><th>niche</th><th>MGS ±</th><th>fad</th><th>$/mo P50</th></tr>"
        f"{faller_rows or '<tr><td colspan=5>No fallers.</td></tr>'}</table>"
    )
    return _shell(
        title="movers",
        active="movers.html",
        heading="Movers",
        intro="New ideas and the biggest MGS moves since the previous scored run.",
        body=body,
        moment=moment,
    )


def _run_line(run: Run | None, moment: datetime, ideas: int, niches: int) -> str:
    if run is None:
        return f"No finished nightly run found. Showing {ideas} ideas in {niches} niches."
    started = run.started_at.isoformat() if run.started_at else "?"
    return (
        f"Run {html.escape(str(run.id))[:8]} ({html.escape(run.status)}, "
        f"started {html.escape(started)}): {ideas} ideas in {niches} niches."
    )


def _pain_badge(pain: float | None) -> str:
    if pain is None:
        return '<span class="badge pain-na">pain: not assessed</span>'
    if pain >= _PAIN_HIGH:
        cls = "pain-high"
    elif pain >= _PAIN_MID:
        cls = "pain-mid"
    else:
        cls = "pain-low"
    return f'<span class="badge {cls}">pain: {pain:.0f}</span>'


def _niche_card(
    *,
    niche: dict[str, Any],
    judgements: Mapping[int, Any],
    briefs: Mapping[tuple[str, str], Mapping[str, Any]],
    evidence: Mapping[str, dict[str, Any]],
    dropped: Sequence[dict[str, str]],
    dropped_by_phrase: Mapping[str, dict[str, str]],
    rank: int,
) -> str:
    category = html.escape(str(niche["category"]))
    problem = html.escape(str(niche.get("problem") or "—"))
    rationale = html.escape(str(niche.get("rationale") or "Not assessed this run."))
    rows = []
    for position, item in enumerate(niche["items"], start=1):
        rows.append(
            _idea_row(
                item=item,
                position=position,
                judgements=judgements,
                briefs=briefs,
                evidence=evidence.get(item.phrase, {}),
                l1_drop=(dropped_by_phrase.get(item.phrase) or {}).get("reason", ""),
            )
        )
    table = (
        "<table><tr><th>#</th><th>idea</th><th>MGS</th><th>CI</th><th>pain&nbsp;lens</th>"
        "<th>fad</th><th>$/mo P50</th><th>seen</th><th>status</th></tr>"
        + "".join(rows)
        + "</table>"
        if rows
        else "<p class='cut'>No scored products this window — pain assessed from niche chatter.</p>"
    )
    cut = ""
    if dropped:
        shown = "".join(
            f"<li>{html.escape(d['phrase'])} — {html.escape(d['reason'])}</li>"
            for d in dropped[:_CUT_LIST_SHOWN]
        )
        rest = len(dropped) - _CUT_LIST_SHOWN
        extra = f"<li>…and {rest} more</li>" if rest > 0 else ""
        cut = (
            f"<details><summary>Didn't make the cut ({len(dropped)})</summary>"
            f"<ul class='cut'>{shown}{extra}</ul></details>"
        )
    return f"""<section class="niche">
<h2>#{rank} {category}</h2>
<div class="scores">{_pain_badge(niche["pain"])}
<span>passion: {niche["passion"]:.0f}</span>
<span>niche score: {niche["score"]:.1f}</span></div>
<p class="problem">{problem}</p>
<p class="rationale">{rationale}</p>
{table}
{cut}
</section>"""


def _idea_row(
    *,
    item: RankedScore,
    position: int,
    judgements: Mapping[int, Any],
    briefs: Mapping[tuple[str, str], Mapping[str, Any]],
    evidence: Mapping[str, Any],
    l1_drop: str,
) -> str:
    judgement = judgements.get(item.candidate_id)
    brief = briefs.get((item.phrase, item.category))
    if judgement is not None:
        status = "judge: " + str(getattr(judgement, "decision", "pending"))
    elif l1_drop:
        status = "below shortlist"
    else:
        status = "unjudged"
    interest = "—" if item.interest is None else f"{item.interest:.0f}"
    detail = [
        f"<div>MGS {item.mgs:.1f} (DV {item.demand_velocity:.0f} · "
        f"SS {item.saturation:.0f} · SP {item.buyer_pain:.0f} · "
        f"MP {item.money:.0f} · FE {item.feasibility:.0f} · CI {interest}) "
        f"[weights {html.escape(item.weights_version)}]</div>"
    ]
    if judgement is not None and getattr(judgement, "reason", ""):
        decision = html.escape(str(getattr(judgement, "decision", "")))
        confidence = float(getattr(judgement, "confidence", 0) or 0)
        detail.append(
            f"<div><b>Judge ({decision}, conf {confidence:.2f}):</b> "
            f"{html.escape(str(judgement.reason))}</div>"
        )
    if l1_drop:
        detail.append(f"<div><b>Shortlist:</b> {html.escape(l1_drop)}</div>")
    if brief is not None:
        detail.append(f"<div><b>Brief:</b> {html.escape(str(brief.get('verdict', '')))}</div>")
    quotes = evidence.get("quotes", []) if isinstance(evidence, dict) else []
    if quotes:
        blocks = "".join(f"<blockquote>{html.escape(q)}</blockquote>" for q in quotes)
        detail.append(f"<div>People said:{blocks}</div>")
    urls = evidence.get("urls", []) if isinstance(evidence, dict) else []
    if urls:
        links = "".join(
            f'<div><a href="{html.escape(u)}">{html.escape(u[:80])}</a></div>'
            for u in urls
        )
        detail.append(f"<div>Sources:{links}</div>")
    sources = sorted(evidence.get("sources", set())) if isinstance(evidence, dict) else []
    mentions = evidence.get("mentions", 0) if isinstance(evidence, dict) else 0
    detail.append(
        f"<div class='ev'>Found in: {html.escape(', '.join(sources) or '—')} · "
        f"{mentions} lake mentions</div>"
    )
    return (
        f"<tr><td>{position}</td><td><b>{html.escape(item.phrase)}</b>"
        f"<details><summary>evidence &amp; why</summary>{''.join(detail)}</details></td>"
        f"<td>{item.mgs:.1f}</td>"
        f"<td>{interest}</td>"
        f"<td>SP {item.buyer_pain:.0f}</td>"
        f"<td>{html.escape(item.fad_label)} ({item.fad_probability:.2f})</td>"
        f"<td>{item.revenue_p50:,.0f}</td>"
        f"<td>{item.mentions}</td>"
        f"<td>{html.escape(status)}</td></tr>"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Build the dashboard from the configured database: `python -m scripts.dashboard`."""
    parser = argparse.ArgumentParser(prog="python -m scripts.dashboard")
    parser.add_argument("--out", default="dashboard", help="output directory")
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--window-days", type=int, default=90)
    args = parser.parse_args(argv)
    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    try:
        settings = load_settings(config_dir)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return 1
    try:
        engine = create_db_engine(settings)
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}")
        return 1
    try:
        target = build_dashboard(
            create_session_factory(engine),
            out_dir=Path(args.out),
            window_days=args.window_days,
        )
    finally:
        engine.dispose()
    print(f"dashboard: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
