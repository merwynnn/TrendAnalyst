"""The database schema (spec §7) — ten tables, typed.

Two rules from the spec shape every decision here:

* **Snapshots are append-only and history is never updated in place** (§5.3). `scores`
  and `briefs` carry database triggers that reject UPDATE and DELETE outright, so a bug
  in application code cannot rewrite history — it can only add a correction row.
* **State stores references and hashes, never blobs** (brief §4). `raw_items` keeps the
  payload hash and cursor; payloads are bounded at the plugin boundary.

Timestamps are `timestamptz` and always UTC. The naming convention is fixed so Alembic
produces stable, reviewable names instead of auto-generated ones.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "Brief",
    "Candidate",
    "EvalCase",
    "LLMCache",
    "RawItem",
    "Run",
    "RunSourceLog",
    "Score",
    "SignalRow",
    "Source",
]

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with a deterministic constraint-naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _utc_now_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# Registry mirror and run bookkeeping
# ---------------------------------------------------------------------------
class Source(Base):
    """Mirror of `config/sources.yaml` (spec §7) — plus the per-source watermark."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(String(1), nullable=False)
    layers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    schedule: Mapped[str] = mapped_column(String(16), nullable=False)
    budget_per_day: Mapped[int] = mapped_column(Integer, nullable=False)
    rps: Mapped[float] = mapped_column(Float, nullable=False)
    cache_ttl_h: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    domains: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    #: Cursor or last-fetch marker. A rerun processes only what changed since it.
    watermark: Mapped[str | None] = mapped_column(Text, nullable=True)
    watermark_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = _utc_now_column()

    __table_args__ = (
        CheckConstraint("tier IN ('S', 'A')", name="tier_known"),
        CheckConstraint("budget_per_day > 0", name="budget_positive"),
        CheckConstraint("rps > 0", name="rps_positive"),
        # The registry invariant, mirrored in the database: Tier A never runs in L0.
        CheckConstraint("NOT (tier = 'A' AND 'L0' = ANY(layers))", name="tier_a_never_in_l0"),
    )


class Run(Base):
    """One pipeline execution (spec §7: id, started/finished, status per layer)."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="nightly")
    #: {"L0": {"status": "ok", "items": 1234, ...}, "L1": {...}} — per-layer progress.
    layer_status: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'ok', 'degraded', 'failed', 'aborted', 'empty')",
            name="status_known",
        ),
        CheckConstraint(
            "trigger IN ('nightly', 'hourly', 'manual', 'resume')", name="trigger_known"
        ),
        Index("ix_runs_started_at", "started_at"),
    )


class RunSourceLog(Base):
    """Per (run, source) outcome — one log row per source per run."""

    __tablename__ = "run_source_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quota_spent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rate_limit_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Why a source was skipped or degraded, in words — never a silent zero.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "source_id", name="uq_run_source_log_run_id"),
        CheckConstraint(
            "status IN ('ok', 'degraded', 'failed', 'skipped')", name="status_known"
        ),
        Index("ix_run_source_log_source_id_started_at", "source_id", "started_at"),
    )


# ---------------------------------------------------------------------------
# The lake and the normalized facts
# ---------------------------------------------------------------------------
class RawItem(Base):
    """Raw lake row (spec §7). 90-day TTL — the only table whose rows are deleted."""

    __tablename__ = "raw_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Bounded, already-parsed payload. Bounded is enforced at the plugin boundary —
    #: state stores references and hashes, not lakes (brief §4).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        # The dedup key of spec §5.2: an identical hash means skip parse, skip score,
        # skip LLM. Unique per source so a rerun cannot re-store the same payload.
        UniqueConstraint("source_id", "content_hash", name="uq_raw_items_source_id"),
        Index("ix_raw_items_fetched_at", "fetched_at"),
    )


class SignalRow(Base):
    """Normalized fact (spec §7): entity, source, metric, value, ts."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = _utc_now_column()

    __table_args__ = (
        UniqueConstraint(
            "source_id", "entity", "metric", "ts", name="uq_signals_source_id"
        ),
        Index("ix_signals_entity_metric_ts", "entity", "metric", "ts"),
        Index("ix_signals_category_ts", "category", "ts"),
    )


class Candidate(Base):
    """A phrase or entity under evaluation, with its category (spec §7)."""

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    phrase: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    mentions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = _utc_now_column()
    last_seen_at: Mapped[datetime] = _utc_now_column()

    __table_args__ = (
        UniqueConstraint("phrase", "category", name="uq_candidates_phrase"),
        CheckConstraint(
            "status IN ('active', 'pruned', 'kept', 'briefed', 'dropped')", name="status_known"
        ),
        Index("ix_candidates_category_status", "category", "status"),
    )


# ---------------------------------------------------------------------------
# Append-only outputs: snapshots and briefs
# ---------------------------------------------------------------------------
class Score(Base):
    """Append-only score snapshot (spec §5.3, §7).

    Every run appends a versioned row (candidate, run, weights version, sub-scores,
    fad probability, revenue triple). Nothing ever updates it: trends, backtests and
    weight learning read this table's history. A database trigger enforces that.
    """

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    scored_at: Mapped[datetime] = _utc_now_column()
    #: Which weights produced this row — without it, history cannot be compared.
    weights_version: Mapped[str] = mapped_column(String(32), nullable=False)

    demand_velocity: Mapped[float] = mapped_column(Float, nullable=False)  # DV
    saturation: Mapped[float] = mapped_column(Float, nullable=False)  # SS (inverted in MGS)
    buyer_pain: Mapped[float] = mapped_column(Float, nullable=False)  # SP
    money: Mapped[float] = mapped_column(Float, nullable=False)  # MP
    feasibility: Mapped[float] = mapped_column(Float, nullable=False)  # FE
    #: Current interest (v2+): raw mention + engagement heat, global percentile. NULL on
    #: v1 rows, which predate it — readers must treat NULL as "unmeasured", never 0.
    interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: MGS v1 = 0.30*DV + 0.25*(100-SS) + 0.20*SP + 0.15*MP + 0.10*FE;
    #: v2 = 0.25*DV + 0.20*(100-SS) + 0.15*SP + 0.10*MP + 0.10*FE + 0.20*CI.
    #: Computed in code; the weights version says which formula a row used.
    mgs: Mapped[float] = mapped_column(Float, nullable=False)

    fad_probability: Mapped[float] = mapped_column(Float, nullable=False)
    fad_label: Mapped[str] = mapped_column(String(16), nullable=False)

    revenue_p10: Mapped[float] = mapped_column(Float, nullable=False)
    revenue_p50: Mapped[float] = mapped_column(Float, nullable=False)
    revenue_p90: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "run_id", "weights_version", name="uq_scores_candidate_id"
        ),
        CheckConstraint("mgs >= 0 AND mgs <= 100", name="mgs_in_range"),
        CheckConstraint(
            "revenue_p10 <= revenue_p50 AND revenue_p50 <= revenue_p90", name="revenue_ordered"
        ),
        CheckConstraint(
            "fad_probability >= 0 AND fad_probability <= 1", name="fad_probability_in_range"
        ),
        CheckConstraint("fad_label IN ('fad', 'trend', 'evergreen')", name="fad_label_known"),
        Index("ix_scores_run_id_mgs", "run_id", "mgs"),
    )


class Brief(Base):
    """Writer output, linked to the score snapshot that justified it (spec §7)."""

    __tablename__ = "briefs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    score_id: Mapped[int] = mapped_column(
        ForeignKey("scores.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _utc_now_column()
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    #: Every factual claim in the brief points at a citation (quote + URL), enforced in
    #: code before the row is written (spec §6.3).
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        # One brief per candidate per run: a resumed run cannot double-emit.
        UniqueConstraint("run_id", "candidate_id", name="uq_briefs_run_id"),
    )


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------
class LLMCache(Base):
    """Hash -> gate output, 30-day TTL (spec §6.3). Identical work never pays twice."""

    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    gate: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _utc_now_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("gate IN ('planner', 'judge', 'writer')", name="gate_known"),
        Index("ix_llm_cache_expires_at", "expires_at"),
    )


class Judgement(Base):
    """One gate's verdict on one candidate, with the citations that justified it.

    Append-only (migration 0002, deviation D12): the specification's §7 table list has nowhere to
    put §6.2's *"keep/drop + fad probability + enrich list, JSON, cited quotes"*, and the LLM cache
    expires after 30 days — so the advice would die while the decision it justified lived on.
    `candidates.status` carries the current state for cheap queries; this table is the history.
    """

    __tablename__ = "judgements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    gate: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    fad_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fad_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Quotes that survived the code-enforced grounding rule (spec §6.3).
    quotes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    #: What the Judge asked L2 to fetch (Tier-A, P4).
    enrich: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    ungrounded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dropped_quotes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    cache_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _utc_now_column()

    __table_args__ = (
        UniqueConstraint("candidate_id", "run_id", "gate", name="uq_judgements_candidate"),
        CheckConstraint("gate IN ('planner', 'judge', 'writer')", name="gate_known"),
        CheckConstraint("decision IN ('keep', 'drop')", name="decision_known"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_range"),
        CheckConstraint(
            "fad_label IS NULL OR fad_label IN ('fad', 'trend', 'evergreen')",
            name="fad_label_known",
        ),
        CheckConstraint("dropped_quotes >= 0", name="dropped_quotes_non_negative"),
        Index("ix_judgements_candidate_created", "candidate_id", "created_at"),
        Index("ix_judgements_run_id", "run_id"),
    )


class EvalCase(Base):
    """Golden case with the behaviour the system must show on it (spec §8)."""

    __tablename__ = "eval_cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    input_signals: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    expected_keep: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected_fad_label: Mapped[str] = mapped_column(String(16), nullable=False)
    score_min: Mapped[float] = mapped_column(Float, nullable=False)
    score_max: Mapped[float] = mapped_column(Float, nullable=False)
    #: Where the case came from (run id + date). Provenance matters: invented cases are
    #: worse than no cases.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    baseline_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("score_min <= score_max", name="score_band_ordered"),
        CheckConstraint(
            "expected_fad_label IN ('fad', 'trend', 'evergreen')", name="fad_label_known"
        ),
    )
