---
# TrendAnalyst-aq98
title: 'P7 — top of the funnel: noun-phrase mining, language gate, measured output quality'
status: completed
type: epic
created_at: 2026-09-20T09:26:12Z
updated_at: 2026-09-20T09:26:12Z
---

P7 — the top of the funnel: is the output worth reading?

Not a phase from either dossier. It answers the human's question after the first live nights:
"run it for real and say honestly whether anything in the results is a real product gap."

Answer: no — 67% of the Judge's drops were sentence fragments, and the ranked table held 2 real
product nouns in 25 rows. The gates were fine; the mining layer was emitting substrings of sentences
and the taxonomy matched 1% of the lake.

Fixed: a head-noun rule (a phrase's category comes from its head, which must be a buyable noun, or a
shared product-form noun with the category from the modifiers); a 61-entry product_nouns vocabulary
(17 of which the rule needs and no category had); a 33-entry brand blocklist; a document-level
language gate (Turkish/German/Spanish/... markers); a per-reason rejection report in the ledger line;
and the apostrophe fix in normalize_phrase.

Measured outcome: 10 of 10 candidates in the final run are product nouns; Judge keep rate 17% -> 38%;
fragment share of drops 67% -> 0-20%; and the drops are now about substance ("too broad and highly
competitive", "standard commodity") rather than syntax.

Also found and fixed: the offline replay broke after a live night (the fixture key contained the
watermark, so a real run invalidated its own recording); and 13 of the 50 golden cases were
mislabelled by the provisional threshold the Judge was always meant to replace.

Honest gaps: the lake is the binding constraint (1,181 texts -> 79 candidates -> 10 kept, 5 of them
from a single document; 12 registered Tier-S sources still have no plugin); the Judge is charitable to
grammatical-but-empty phrases ("excellent filament" kept at 0.70); the taxonomy remains small.
