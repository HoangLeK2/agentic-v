"""Candidate-level learning review and explicit promotion."""

import json
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from os import getenv
from typing import Literal

from sqlalchemy import create_engine, text

from db import create_knowledge, db_url

LearningNamespace = Literal["engineering", "growth", "research", "global"]
LearningVerdict = Literal["PASS", "FAIL"]


def _table() -> str:
    schema = getenv("WORKFORCE_LEARNING_SCHEMA", "ai")
    if not schema.replace("_", "").isalnum():
        raise RuntimeError("WORKFORCE_LEARNING_SCHEMA is invalid")
    return f"{schema}.workforce_learning_candidates"


def _ensure_table(conn) -> None:
    conn.execute(
        text(
            f"""CREATE TABLE IF NOT EXISTS {_table()} (
                id varchar(32) PRIMARY KEY,
                namespace varchar(32) NOT NULL,
                insight text NOT NULL,
                evidence jsonb NOT NULL,
                source_run_id varchar(255),
                verdict varchar(8),
                rationale text,
                promotion_started_at timestamptz,
                promoted_at timestamptz,
                created_at timestamptz NOT NULL
            )"""
        )
    )
    conn.execute(text(f"ALTER TABLE {_table()} ADD COLUMN IF NOT EXISTS promotion_started_at timestamptz"))
    conn.execute(text(f"ALTER TABLE {_table()} ADD COLUMN IF NOT EXISTS fingerprint varchar(64)"))
    conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS workforce_learning_fingerprint_idx ON {_table()} (fingerprint)"))


def propose_learning_candidate(
    namespace: LearningNamespace,
    insight: str,
    evidence: list[str],
    source_run_id: str | None = None,
) -> dict:
    """Persist a reusable insight as an unverified candidate, never as learned knowledge."""
    if not insight.strip() or not evidence:
        raise ValueError("A learning candidate requires an insight and evidence")
    normalized = " ".join(insight.lower().split())
    fingerprint = hashlib.sha256(f"{namespace}:{normalized}".encode()).hexdigest()
    candidate_id = uuid.uuid4().hex
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            _ensure_table(conn)
            result = conn.execute(
                text(
                    f"INSERT INTO {_table()} "
                    "(id, namespace, insight, evidence, source_run_id, fingerprint, created_at) "
                    "VALUES (:id, :namespace, :insight, CAST(:evidence AS jsonb), :source_run_id, :fingerprint, "
                    ":created_at) ON CONFLICT (fingerprint) DO NOTHING"
                ),
                {
                    "id": candidate_id,
                    "namespace": namespace,
                    "insight": insight.strip(),
                    "evidence": json.dumps(evidence),
                    "source_run_id": source_run_id,
                    "fingerprint": fingerprint,
                    "created_at": datetime.now(UTC),
                },
            )
            if result.rowcount == 0:
                existing_id = conn.execute(
                    text(f"SELECT id FROM {_table()} WHERE fingerprint=:fingerprint"),
                    {"fingerprint": fingerprint},
                ).scalar_one()
                return {"learning_candidate_id": existing_id, "status": "duplicate", "namespace": namespace}
    finally:
        engine.dispose()
    return {"learning_candidate_id": candidate_id, "status": "pending", "namespace": namespace}


def learning_status() -> dict:
    """Return aggregate candidate and promoted-learning counts without exposing content."""
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            _ensure_table(conn)
            rows = conn.execute(
                text(
                    f"SELECT namespace, count(*) AS total, "
                    "count(*) FILTER (WHERE verdict IS NULL) AS pending, "
                    "count(*) FILTER (WHERE verdict='FAIL') AS rejected, "
                    "count(*) FILTER (WHERE promoted_at IS NOT NULL) AS promoted "
                    f"FROM {_table()} GROUP BY namespace ORDER BY namespace"
                )
            ).mappings()
            return {"namespaces": [dict(row) for row in rows]}
    finally:
        engine.dispose()


def evaluate_learning_candidate(candidate_id: str, verdict: LearningVerdict, rationale: str) -> dict:
    """Attach an independent PASS or FAIL verdict to exactly one candidate id."""
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            _ensure_table(conn)
            result = conn.execute(
                text(
                    f"UPDATE {_table()} SET verdict=:verdict, rationale=:rationale "
                    "WHERE id=:id AND promoted_at IS NULL AND promotion_started_at IS NULL"
                ),
                {"id": candidate_id, "verdict": verdict, "rationale": rationale.strip()},
            )
            if result.rowcount != 1:
                raise ValueError("Unknown or already promoted learning_candidate_id")
    finally:
        engine.dispose()
    return {"learning_candidate_id": candidate_id, "verdict": verdict}


async def promote_learning_candidate(candidate_id: str) -> dict:
    """Promote exactly one independently verified candidate into its namespace."""
    engine = create_engine(db_url)
    row = None
    try:
        with engine.begin() as conn:
            _ensure_table(conn)
            row = (
                conn.execute(
                    text(
                        f"UPDATE {_table()} SET promotion_started_at=:started_at "
                        "WHERE id=:id AND verdict='PASS' AND promoted_at IS NULL "
                        "AND (promotion_started_at IS NULL OR promotion_started_at < :stale_before) "
                        "RETURNING namespace, insight, evidence, source_run_id, verdict, promoted_at"
                    ),
                    {
                        "id": candidate_id,
                        "started_at": datetime.now(UTC),
                        "stale_before": datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10),
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                current = (
                    conn.execute(
                        text(f"SELECT verdict, promoted_at, promotion_started_at FROM {_table()} WHERE id=:id"),
                        {"id": candidate_id},
                    )
                    .mappings()
                    .first()
                )
                if current is None:
                    raise ValueError("Unknown learning_candidate_id")
                if current["promoted_at"] is not None:
                    return {"learning_candidate_id": candidate_id, "status": "already_promoted"}
                if current["verdict"] != "PASS":
                    raise ValueError("Only a candidate-level PASS can be promoted")
                raise ValueError("Learning candidate promotion is already in progress")
            if row["promoted_at"] is not None:
                return {"learning_candidate_id": candidate_id, "status": "already_promoted"}
            if row["verdict"] != "PASS":
                raise ValueError("Only a candidate-level PASS can be promoted")

        knowledge = create_knowledge(f"{row['namespace'].title()} Learnings", "workforce_learnings")
        try:
            await knowledge.add_content_async(
                name=f"learning-{candidate_id}",
                text_content=row["insight"],
                metadata={
                    "namespace": row["namespace"],
                    "learning_candidate_id": candidate_id,
                    "evidence": row["evidence"],
                    "source_run_id": row["source_run_id"],
                },
                upsert=True,
            )
        except Exception:
            with engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE {_table()} SET promotion_started_at=NULL WHERE id=:id AND promoted_at IS NULL"),
                    {"id": candidate_id},
                )
            raise
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {_table()} SET promoted_at=:promoted_at WHERE id=:id AND promotion_started_at IS NOT NULL"
                ),
                {"id": candidate_id, "promoted_at": datetime.now(UTC)},
            )
    finally:
        engine.dispose()
    return {"learning_candidate_id": candidate_id, "status": "promoted", "namespace": row["namespace"]}
