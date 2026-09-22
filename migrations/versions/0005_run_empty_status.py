"""Allow the `empty` run status: ran fine, found nothing.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-21

Why: a decide run over an empty lake — or with no gate transport — closes its run row
with status `empty`, and the 0001 CHECK constraint only knew
running/ok/degraded/failed/aborted. The first `--judge-replay` on a night with no
candidates died on the constraint instead of reporting. `empty` is a legitimate
terminal state (distinct from `degraded`: nothing failed), so the constraint learns it
rather than the callers lying with `ok`.

Raw SQL, not op.drop_constraint/create_check_constraint: those apply the metadata
naming convention to the name and double-prefix it (see 0004).

Downgrade rewrites `empty` rows to `ok` first: re-adding the narrow constraint with
such rows present would fail, and a downgrade must never fail on data a newer revision
wrote.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE runs DROP CONSTRAINT ck_runs_status_known"))
    op.execute(
        sa.text(
            "ALTER TABLE runs ADD CONSTRAINT ck_runs_status_known "
            "CHECK (status IN ('running', 'ok', 'degraded', 'failed', 'aborted', 'empty'))"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE runs SET status = 'ok' WHERE status = 'empty'"))
    op.execute(sa.text("ALTER TABLE runs DROP CONSTRAINT ck_runs_status_known"))
    op.execute(
        sa.text(
            "ALTER TABLE runs ADD CONSTRAINT ck_runs_status_known "
            "CHECK (status IN ('running', 'ok', 'degraded', 'failed', 'aborted'))"
        )
    )
