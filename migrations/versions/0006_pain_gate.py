"""Allow the `pain` gate in `llm_cache.gate`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28

Why: the niche pain assessment caches like every other gate (one call per run, and the
cache is what keeps a re-run free), and the cache write carries the gate name — which
the CHECK constraint limits to planner/judge/writer/extractor. Widening the constraint
is the whole change; no table, index or trigger is touched.

Downgrade deletes `pain` cache rows first: re-adding the narrow constraint with such
rows present would fail, and a downgrade must never fail on data a newer revision
wrote. The rows are re-derivable (re-running the assessment re-caches them).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE llm_cache DROP CONSTRAINT ck_llm_cache_gate_known"))
    op.execute(
        sa.text(
            "ALTER TABLE llm_cache ADD CONSTRAINT ck_llm_cache_gate_known "
            "CHECK (gate IN ('planner', 'judge', 'writer', 'extractor', 'pain'))"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM llm_cache WHERE gate = 'pain'"))
    op.execute(sa.text("ALTER TABLE llm_cache DROP CONSTRAINT ck_llm_cache_gate_known"))
    op.execute(
        sa.text(
            "ALTER TABLE llm_cache ADD CONSTRAINT ck_llm_cache_gate_known "
            "CHECK (gate IN ('planner', 'judge', 'writer', 'extractor'))"
        )
    )
