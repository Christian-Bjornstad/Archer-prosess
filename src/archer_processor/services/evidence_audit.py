from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from archer_processor.core.models import DatabaseEvidence, VariantRecord


AUDIT_SCHEMA_VERSION = 2
RETRYABLE_EVIDENCE_STATUSES = frozenset(
    {
        "error",
        "login_required",
        "rate_limited",
        "timeout",
        "unauthorized",
        "identity_mismatch",
        "partial_capture",
        "verification_required",
        "quota_exhausted",
        "session_lost",
    }
)


def is_completed_evidence(evidence: DatabaseEvidence) -> bool:
    return evidence.status.strip().casefold() not in RETRYABLE_EVIDENCE_STATUSES


def audit_digest(database: str, variant: VariantRecord) -> str:
    identity = f"{database}|{variant.symbol}|{variant.hgvsc}|{variant.hgvsp}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def write_evidence_audit(
    artifact_directory: Path,
    database: str,
    variant: VariantRecord,
    evidence: DatabaseEvidence,
    *,
    query_attempts: Sequence[str] = (),
    duration_seconds: float = 0.0,
) -> Path:
    artifact_directory.mkdir(parents=True, exist_ok=True)
    path = artifact_directory / f"{audit_digest(database, variant)}.audit.json"
    payload = asdict(evidence)
    payload.update(
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "retryable": not is_completed_evidence(evidence),
            "variant_key": f"{variant.sample}|{variant.hgvsc}",
            "query_attempts": list(query_attempts),
            "duration_seconds": round(max(0.0, duration_seconds), 3),
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def persist_evidence_result(
    artifact_directory: Path,
    database: str,
    variant: VariantRecord,
    evidence: DatabaseEvidence,
    *,
    query_attempts: Sequence[str] = (),
    started_at: float,
    now: Callable[[], float] = time.monotonic,
) -> DatabaseEvidence:
    write_evidence_audit(
        artifact_directory,
        database,
        variant,
        evidence,
        query_attempts=query_attempts,
        duration_seconds=now() - started_at,
    )
    return evidence
