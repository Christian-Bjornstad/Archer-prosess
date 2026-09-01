from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.capture_validation import validate_capture


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
SCREENSHOT_REQUIRED_DATABASES = frozenset(
    {"COSMIC", "OncoKB", "Franklin", "ClinVar", "MTBP"}
)


@dataclass(slots=True)
class EvidenceAuditIndex:
    root: Path
    by_digest: dict[str, list[Path]]

    @classmethod
    def build(cls, root: Path) -> "EvidenceAuditIndex":
        by_digest: dict[str, list[Path]] = defaultdict(list)
        for path in root.rglob("*.audit.json"):
            match = re.match(
                r"([0-9a-f]{16})(?:-[^.]+)?\.audit\.json$",
                path.name,
                flags=re.IGNORECASE,
            )
            if match:
                by_digest[match.group(1).casefold()].append(path)
        return cls(root=root, by_digest=dict(by_digest))

    def candidates(self, database: str, variant: VariantRecord) -> list[Path]:
        digest = audit_digest(database, variant)
        return [
            path
            for path in self.by_digest.get(digest, [])
            if path.parent.name.casefold() == database.casefold()
        ]

    def best(
        self,
        database: str,
        variant: VariantRecord,
    ) -> tuple[Path, dict[str, Any]] | None:
        canonical_name = f"{audit_digest(database, variant)}.audit.json"
        ranked: list[tuple[tuple[int, int, int, float], Path, dict[str, Any]]] = []
        for path in self.candidates(database, variant):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(payload, dict):
                continue
            raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
            verification = raw.get("identity_verification")
            verified = int(
                raw.get("assembly_verified") == "GRCh37"
                or (
                    isinstance(verification, dict)
                    and verification.get("accepted") is True
                )
            )
            screenshots = raw.get("screenshots")
            screenshot_count = sum(
                bool(record.get("path"))
                for record in screenshots
                if isinstance(record, dict)
            ) if isinstance(screenshots, list) else 0
            try:
                modified = path.stat().st_mtime
            except OSError:
                modified = 0.0
            score = (
                int(path.name == canonical_name),
                verified,
                screenshot_count,
                modified,
            )
            ranked.append((score, path, payload))
        if not ranked:
            return None
        _, path, payload = max(ranked, key=lambda item: (item[0], str(item[1])))
        return path, payload


def is_completed_evidence(evidence: DatabaseEvidence) -> bool:
    if evidence.database == "MTBP" and evidence.status == "found":
        cleanup = evidence.raw.get("remote_report_cleanup")
        if isinstance(cleanup, dict) and cleanup.get("status"):
            return cleanup["status"] in {"deleted", "already_absent"}
    return evidence.status.strip().casefold() not in RETRYABLE_EVIDENCE_STATUSES


def migrate_loaded_evidence(
    evidence: DatabaseEvidence,
    artifact_root: Path,
) -> DatabaseEvidence:
    if (
        evidence.database == "ClinVar"
        and evidence.status == "found"
        and evidence.raw.get("assembly_verified") != "GRCh37"
    ):
        evidence.status = "verification_required"
        evidence.summary = "Legacy ClinVar result requires explicit GRCh37 verification."
    _rebase_screenshot_paths(evidence.raw, artifact_root)
    if (
        evidence.status == "found"
        and evidence.database in SCREENSHOT_REQUIRED_DATABASES
    ):
        records = evidence.raw.get("screenshots")
        paths = [
            Path(record["path"])
            for record in records
            if isinstance(record, dict) and record.get("path")
        ] if isinstance(records, list) else []
        if not paths and evidence.raw.get("screenshot"):
            paths = [Path(str(evidence.raw["screenshot"]))]
        if not paths or any(not validate_capture(path).valid for path in paths):
            evidence.status = "partial_capture"
            evidence.summary = f"{evidence.database} requires screenshot recapture."
    return evidence


def _rebase_screenshot_paths(raw: dict[str, Any], artifact_root: Path) -> None:
    if raw.get("screenshot"):
        raw["screenshot"] = _rebase_path(str(raw["screenshot"]), artifact_root)
    screenshots = raw.get("screenshots")
    if isinstance(screenshots, list):
        for record in screenshots:
            if isinstance(record, dict) and record.get("path"):
                record["path"] = _rebase_path(str(record["path"]), artifact_root)


def _rebase_path(value: str, artifact_root: Path) -> str:
    path = Path(value)
    if path.exists():
        return str(path)
    parts = path.parts
    for index, part in enumerate(parts):
        if part.casefold().endswith("_browser_evidence"):
            candidate = artifact_root.joinpath(*parts[index + 1 :])
            if candidate.exists():
                return str(candidate)
            break
    return value


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
