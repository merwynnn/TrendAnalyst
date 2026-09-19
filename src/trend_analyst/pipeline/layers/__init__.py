"""The four pipeline layers, and the two of them a *source* can be declared in.

Spec §2 runs L0 Collect -> L1 Mine -> L2 Enrich -> L3 Decide. Sources only ever appear
in two of those:

* **L0** — broad, cheap, keyless collection. Tier S only.
* **L2** — enrichment of the survivors of L1, top-K only. Tier A lives here, and only
  here, because that is where quota is spent (spec §4.3: a Tier-A source declaring L0
  is a hard startup failure).

This module is the single home of those identifiers: the registry validates against
them, and the orchestrator will iterate them in order from P1 on. Keeping the taxonomy
here (rather than inside the registry) means the pipeline never has to import a
validation module to know what a layer is.
"""

from __future__ import annotations

from typing import Final, Literal

__all__ = [
    "LAYER_IDS",
    "SOURCE_LAYER_IDS",
    "PipelineLayer",
    "SourceLayer",
    "layer_label",
]

#: Every layer of the pipeline, in execution order.
PipelineLayer = Literal["L0", "L1", "L2", "L3"]
LAYER_IDS: Final[tuple[PipelineLayer, ...]] = ("L0", "L1", "L2", "L3")

#: The subset a source plugin may declare in sources.yaml, in execution order.
SourceLayer = Literal["L0", "L2"]
SOURCE_LAYER_IDS: Final[tuple[SourceLayer, ...]] = ("L0", "L2")

_LABELS: Final[dict[str, str]] = {
    "L0": "Collect — broad, cheap, keyless (Tier S)",
    "L1": "Mine — phrases, velocity, prune 95 %",
    "L2": "Enrich — deep data on top-K survivors only (Tier A)",
    "L3": "Decide — score, fad flag, revenue, brief",
}


def layer_label(layer: str) -> str:
    """Human-readable description of a layer, for health output and logs."""
    return _LABELS.get(layer, f"unknown layer {layer!r}")
