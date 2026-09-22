"""Drop the `quota_ledger` table and its daily-spend view.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21

Why: the cleanup removed the quota ledger's only readers (the resume compare-and-swap
and the reconciliation report) and its only writers (per-source spend recording and the
LLM token log). What remained was a write-only table with no consumer — history that
nobody reads is clutter, not evidence. Per-run limits live in the in-process
`SourceBudget`; spend-per-gate visibility comes from the `llm_cache` token columns.

The downgrade recreates the table, its trigger, its index and the view exactly as 0001
defined them, so the base→head→base round trip still restores everything.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS quota_spend_daily")
    op.execute("DROP TRIGGER IF EXISTS trg_quota_ledger_append_only ON quota_ledger")
    op.execute("DROP FUNCTION IF EXISTS ta_quota_ledger_append_only()")
    op.drop_index("ix_quota_ledger_source_id_spend_date", table_name="quota_ledger")
    op.drop_table("quota_ledger")


def downgrade() -> None:
    op.create_table(
        "quota_ledger",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column(
            "spend_date",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint("amount > 0", name=op.f("ck_quota_ledger_amount_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quota_ledger")),
        sa.UniqueConstraint("run_id", "source_id", "operation", name="uq_quota_ledger_run_id"),
    )
    op.create_index(
        "ix_quota_ledger_source_id_spend_date",
        "quota_ledger",
        ["source_id", "spend_date"],
        unique=False,
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ta_quota_ledger_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'table quota_ledger is append-only (spec 5.3/5.4): % is not permitted; '
                'append a correction row instead', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_quota_ledger_append_only
        BEFORE UPDATE OR DELETE ON quota_ledger
        FOR EACH ROW EXECUTE FUNCTION ta_quota_ledger_append_only();
        """
    )
    op.execute(
        """
        CREATE VIEW quota_spend_daily AS
        SELECT source_id,
               date_trunc('day', spend_date AT TIME ZONE 'UTC')::date AS spend_day,
               sum(amount)::bigint AS requests,
               count(*)::bigint AS ledger_rows
        FROM quota_ledger
        GROUP BY 1, 2;
        """
    )
