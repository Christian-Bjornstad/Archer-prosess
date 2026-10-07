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
from archer_processor.services.variant_identity import genomic_identity


AUDIT_SCHEMA_VERSION = 3
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
        "token_required",
        "layout_changed",
        "transient",
        "submission_unknown",
        "deferred",
    }
)
COMPLETED_EVIDENCE_STATUSES = frozenset(
    {
        "found",
        "not_found",
        "not_applicable",
        "invalid_query",
        "manual_review",
        "manual",
        "unsupported_query",
        "skipped",
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
        digests = {audit_digest(database, variant), _legacy_audit_digest(database, variant)}
        return [
            path
            for digest in digests
            for path in self.by_digest.get(digest, [])
            if path.parent.name.casefold() == database.casefold()
        ]

    def best(
        self,
        database: str,
        variant: VariantRecord,
        *,
        patient_index: int | None = None,
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
            if not _audit_matches_variant(payload, path, database, variant, patient_index):
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
    analysis_id = str(evidence.raw.get("analysis_id") or "")
    if (
        evidence.database == "MTBP"
        and evidence.status == "found"
        and analysis_id.startswith("ARCHER-")
    ):
        cleanup = evidence.raw.get("remote_report_cleanup")
        cleanup_status = cleanup.get("status") if isinstance(cleanup, dict) else ""
        return cleanup_status in {"deleted", "already_absent"}
    return evidence.status.strip().casefold() in COMPLETED_EVIDENCE_STATUSES


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
    identity = json.dumps(
        [database, _requested_identity(database, variant)],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _legacy_audit_digest(database: str, variant: VariantRecord) -> str:
    identity = f"{database}|{variant.symbol}|{variant.hgvsc}|{variant.hgvsp}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _requested_identity(database: str, variant: VariantRecord) -> dict[str, Any]:
    identity = {
        "sample": variant.sample,
        "symbol": variant.symbol.strip().upper(),
        "hgvsc": variant.hgvsc.strip(),
        "hgvsp": variant.hgvsp.strip(),
        "transcript": variant.transcript.strip(),
        "genomic_location": re.sub(r"\s+", "", variant.genomic_location).casefold(),
        "ref_allele": re.sub(r"\s+", "", variant.ref_allele).upper(),
        "alt_allele": re.sub(r"\s+", "", variant.alt_allele).upper(),
    }
    if database == "COSMIC":
        identity["cosmic_ids"] = list(dict.fromkeys(
            match.group(0).upper()
            for match in re.finditer(r"\bCOS(?:M|V)\d+\b", variant.cosmic_id or "", re.I)
        ))
    return identity


def _audit_matches_variant(
    payload: dict[str, Any], path: Path, database: str,
    variant: VariantRecord, patient_index: int | None,
) -> bool:
    if payload.get("database") not in {None, "", database}:
        return False
    saved_key = payload.get("variant_key")
    if saved_key is not None and saved_key != f"{variant.sample}|{variant.hgvsc}":
        return False
    requested = payload.get("requested_identity")
    if requested is not None:
        return requested == _requested_identity(database, variant)
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, int) and schema_version >= AUDIT_SCHEMA_VERSION:
        return False
    if saved_key is None:
        # Identity-poor legacy captures cannot follow another patient's newer file.
        if patient_index is None or path.parent.parent.name != f"patient-{patient_index:03d}":
            return False
    raw = payload.get("raw")
    verification = raw.get("identity_verification") if isinstance(raw, dict) else None
    if isinstance(verification, dict):
        expected = genomic_identity(variant)
        saved_genomic = verification.get("requested")
        if saved_genomic is None and verification.get("accepted") is True:
            saved_genomic = verification.get("returned")
        if isinstance(saved_genomic, dict):
            if expected is None:
                return False
            normalized = asdict(expected)
            for key, value in saved_genomic.items():
                if key not in normalized:
                    continue
                if key == "position":
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        return False
                elif key == "chromosome":
                    value = str(value).upper().removeprefix("CHR")
                    if value == "MT":
                        value = "M"
                elif key in {"reference", "alternate"}:
                    value = re.sub(r"\s+", "", str(value)).upper()
                if value != normalized[key]:
                    return False
    return True


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
            "requested_identity": _requested_identity(database, variant),
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
