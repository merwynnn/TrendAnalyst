"""Add the `judgements` table: durable, append-only gate output with its citations.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19

Why a table the specification's §7 list does not name (deviation D12): §6.2 makes the Judge's
output *"keep/drop + fad probability + enrich list, JSON, cited quotes"*, and §6.3 makes grounding
a code-enforced rule — but the table list has nowhere to put either. The alternatives were to
stuff JSON into `candidates.status` (unqueryable, no citations, no provenance) or to let the
verdict live only in the LLM cache (a 30-day TTL, so the advice would expire while the decision it
justified did not). So the judgement gets a table, and it is append-only like `scores`: a verdict
is a fact about a moment, and a corrected verdict is a new row, never an edit.

`candidates.status` still carries the current state (`kept`/`dropped`) because every query wants
that without a join; the table is the history and the evidence behind it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def _create_append_only_trigger(table: str) -> None:
    """Reject UPDATE and DELETE outright, whatever the application believes."""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION ta_{table}_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'table {table} is append-only (spec 5.3/5.4): % is not permitted; '
                'append a correction row instead', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{table}_append_only
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION ta_{table}_append_only();
        """
    )


def _drop_append_only_trigger(table: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute(f"DROP FUNCTION IF EXISTS ta_{table}_append_only()")


def upgrade() -> None:
    op.create_table(
        "judgements",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "candidate_id",
            sa.BigInteger(),
            sa.ForeignKey("candidates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("gate", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=8), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("fad_label", sa.String(length=16), nullable=True),
        sa.Column("fad_probability", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        #: Cited quotes (text + URL) that survived the grounding rule, and what the Judge asked
        #: L2 to fetch. JSONB so a quote keeps its own shape without a join.
        sa.Column("quotes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enrich", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        #: True when every quote was stripped: the verdict stands, its citations do not.
        sa.Column("ungrounded", sa.Boolean(), nullable=False),
        sa.Column("dropped_quotes", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("cache_key", sa.String(length=64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("gate IN ('planner', 'judge', 'writer')", name="gate_known"),
        sa.CheckConstraint("decision IN ('keep', 'drop')", name="decision_known"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_range"),
        sa.CheckConstraint(
            "fad_label IS NULL OR fad_label IN ('fad', 'trend', 'evergreen')",
            name="fad_label_known",
        ),
        sa.CheckConstraint(
            "fad_probability IS NULL OR (fad_probability >= 0 AND fad_probability <= 1)",
            name="fad_probability_in_range",
        ),
        sa.CheckConstraint("dropped_quotes >= 0", name="dropped_quotes_non_negative"),
        # One judgement per (candidate, run, gate): a resumed run cannot double-emit, and a
        # re-judged candidate in a *later* run is a new row.
        sa.UniqueConstraint("candidate_id", "run_id", "gate", name="uq_judgements_candidate"),
    )
    op.create_index("ix_judgements_candidate_created", "judgements", ["candidate_id", "created_at"])
    op.create_index("ix_judgements_run_id", "judgements", ["run_id"])
    _create_append_only_trigger("judgements")


def downgrade() -> None:
    _drop_append_only_trigger("judgements")
    op.drop_index("ix_judgements_run_id", table_name="judgements")
    op.drop_index("ix_judgements_candidate_created", table_name="judgements")
    op.drop_table("judgements")
