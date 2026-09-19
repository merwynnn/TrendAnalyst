"""Brief rendering: the Writer's structured output turned into one page of markdown.

The Writer gate returns JSON (verdict, players, risks, angles, revenue reasoning, citations); this
module turns that into the page a human reads. It is deliberately **separate from the gate and free
of any model**: rendering is deterministic code, so the same brief always reads the same way, and
a rendering bug is a code bug rather than a prompt-tuning mystery.

Two rules the renderer enforces, both consequences of spec §6.3's grounding rule:

* **A claim with no surviving citation is labelled as such.** If every quote was stripped, the page
  says `no surviving citation` instead of presenting the prose as sourced. The verdict stands; its
  authority is what changes.
* **Citations are listed with their URLs**, because a brief whose reader cannot check it is a
  brief that will eventually be trusted for the wrong reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from trend_analyst.llm.schemas import WriterBrief

__all__ = ["RenderedBrief", "render_brief", "render_briefs_table"]

#: Section headings, in the order the specification lists the content (§6.2).
_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("players", "Players"),
    ("risks", "Risks"),
    ("angles", "Angles"),
)

_NO_CITATION: Final = (
    "**No surviving citation** — every quote this brief offered was stripped by the grounding "
    "rule (spec §6.3) because the URLs it named are not in the collected evidence. Treat the "
    "prose below as opinion, not as sourced fact."
)


@dataclass(frozen=True, slots=True)
class RenderedBrief:
    """A brief's markdown, plus what the renderer had to work with."""

    markdown: str
    citations: int
    grounded: bool
    sections: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "markdown": self.markdown,
            "citations": self.citations,
            "grounded": self.grounded,
            "sections": list(self.sections),
        }


def render_brief(
    brief: WriterBrief,
    *,
    category: str = "",
    mgs: float | None = None,
    revenue: Mapping[str, float] | None = None,
    weights_version: str = "",
    model: str = "",
    as_of: datetime | None = None,
) -> RenderedBrief:
    """Render one brief as markdown.

    Args:
        brief: the gate's validated output (already grounded — the caller runs the rule first).
        category, mgs, revenue, weights_version: the score snapshot the brief is attached to, so
            the page carries the numbers it was written about rather than numbers from memory.
        model: the provider and model that wrote it. A brief with no authorship recorded cannot be
            compared with a later one.
        as_of: when the brief was written. Passed in, never read from the clock, so a replay
            reproduces the page byte for byte.
    """
    lines: list[str] = [f"# {brief.phrase}", ""]
    meta = [f"**Category**: {category or '—'}"]
    if mgs is not None:
        meta.append(f"**MGS**: {mgs:.1f}")
    if weights_version:
        meta.append(f"**Weights**: {weights_version}")
    if revenue:
        meta.append(
            "**Revenue / month**: "
            f"${revenue.get('p10', 0):,.0f} - ${revenue.get('p50', 0):,.0f} - "
            f"${revenue.get('p90', 0):,.0f} (P10-P50-P90)"
        )
    lines.append(" · ".join(meta))
    lines.append("")

    if not brief.quotes:
        lines.extend([_NO_CITATION, ""])

    if brief.verdict:
        lines.extend(["## Verdict", "", brief.verdict.strip(), ""])

    rendered_sections = _render_sections(lines, brief)

    if brief.revenue_reasoning:
        lines.extend(["## Revenue reasoning", "", brief.revenue_reasoning.strip(), ""])

    if brief.quotes:
        lines.extend(["## Citations", ""])
        for quote in brief.quotes:
            source = f" ({quote.source_id})" if quote.source_id else ""
            lines.append(f"- “{quote.text}”{source} — <{quote.url}>")
        lines.append("")

    footer: list[str] = []
    if model:
        footer.append(f"written by {model}")
    if as_of is not None:
        footer.append(f"at {as_of.isoformat()}")
    if brief.enrich if hasattr(brief, "enrich") else False:  # WriterBrief has no enrich list
        footer.append("enrichment requested")  # pragma: no cover - kept for schema symmetry
    if footer:
        lines.append(f"*{' · '.join(footer)}*")

    return RenderedBrief(
        markdown="\n".join(lines).rstrip() + "\n",
        citations=len(brief.quotes),
        grounded=bool(brief.quotes),
        sections=tuple(rendered_sections),
    )


def _render_sections(lines: list[str], brief: WriterBrief) -> list[str]:
    """Append the bullet sections the brief actually has; return which ones were rendered."""
    rendered: list[str] = []
    for attribute, heading in _SECTIONS:
        items = list(getattr(brief, attribute) or [])
        if not items:
            continue
        rendered.append(attribute)
        lines.extend([f"## {heading}", ""])
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    return rendered


def render_briefs_table(briefs: Sequence[Mapping[str, Any]], *, limit: int = 10) -> str:
    """A console table of stored briefs, for the CLI and for the nightly report."""
    if not briefs:
        return "(no briefs yet: run the Writer gate over kept candidates)"
    header = f"{'$/mo P50':>10}  {'cites':>5}  {'model':<26}  phrase"
    rows = [header, "-" * len(header)]
    for row in briefs[:limit]:
        rows.append(
            f"{float(row.get('revenue_p50', 0)):>10,.0f}  {int(row.get('citations', 0)):>5}  "
            f"{str(row.get('model', ''))[:26]:<26}  {row.get('phrase', '')}"
        )
    return "\n".join(rows)
