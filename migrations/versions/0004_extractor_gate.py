"""Allow the `extractor` gate in `llm_cache.gate`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-21

Why: the Extractor gate caches by input-chunk hash like every other gate (the cache is
what keeps replays deterministic), and the cache write carries the gate name — which the
0001 CHECK constraint limits to planner/judge/writer. Widening the constraint is the
whole change; no table, index or trigger is touched.

Downgrade deletes `extractor` cache rows first: re-adding the narrow constraint with
such rows present would fail, and a downgrade must never fail on data a newer revision
wrote. The rows are re-derivable (re-running the chunks re-caches them).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

#: Raw SQL, not op.drop_constraint/create_check_constraint: those apply the metadata
#: naming convention to the name, which double-prefixes an already-conventional name
#: (`ck_llm_cache_ck_llm_cache_gate_known` — the exact failure this revision first
#: shipped with). Verified against the live database: the constraint is called exactly
#: `ck_llm_cache_gate_known`.


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE llm_cache DROP CONSTRAINT ck_llm_cache_gate_known"))
    op.execute(
        sa.text(
            "ALTER TABLE llm_cache ADD CONSTRAINT ck_llm_cache_gate_known "
            "CHECK (gate IN ('planner', 'judge', 'writer', 'extractor'))"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM llm_cache WHERE gate = 'extractor'"))
    op.execute(sa.text("ALTER TABLE llm_cache DROP CONSTRAINT ck_llm_cache_gate_known"))
    op.execute(
        sa.text(
            "ALTER TABLE llm_cache ADD CONSTRAINT ck_llm_cache_gate_known "
            "CHECK (gate IN ('planner', 'judge', 'writer'))"
        )
    )
