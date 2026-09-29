"""Add the `interest` sub-score column to `scores`.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29

Why: MGS v2 scores current interest (raw mention + engagement heat) alongside the five
original sub-scores. The column is NULLABLE with no default: v1 rows predate interest,
and NULL reads as "unmeasured" while a backfilled 0 would read as "no interest" —
a silent lie about history. Readers (latest_ranked, the dashboard) carry the None.

Downgrade drops the column. v2 rows lose their interest values, but their MGS and the
other five sub-scores survive — the downgrade degrades the score, never the history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE scores ADD COLUMN interest FLOAT"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE scores DROP COLUMN interest"))
