from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path


MAX_AUDITS_TO_INSPECT = 2_000


@dataclass(frozen=True, slots=True)
class RecentAnalysis:
    path: Path
    modified_at: datetime | None
    evidence_present: bool
    resume_available: bool
    valid: bool
    message: str


def inspect_recent_analysis(path: Path | str) -> RecentAnalysis:
    """Inspect local recovery metadata without loading data or contacting providers."""
    workbook = Path(path)
    if workbook.suffix.casefold() != ".xlsx":
        return RecentAnalysis(
            workbook, None, False, False, False, "Recent file is not an Excel workbook."
        )
    try:
        stat = workbook.stat()
    except OSError:
        return RecentAnalysis(
            workbook, None, False, False, False, "Recent workbook is unavailable."
        )

    artifact_root = workbook.parent / f"{workbook.stem}_browser_evidence"
    evidence_present = artifact_root.is_dir()
    resume_available = False
    malformed = 0
    inspected = 0
    if evidence_present:
        try:
            audits = islice(artifact_root.rglob("*.audit.json"), MAX_AUDITS_TO_INSPECT)
            for audit_path in audits:
                inspected += 1
                try:
                    payload = json.loads(audit_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    malformed += 1
                    continue
                if not isinstance(payload, dict):
                    malformed += 1
                    continue
                if payload.get("schema_version") == 2 and payload.get("retryable") is True:
                    resume_available = True
        except OSError:
            malformed += 1

    if malformed:
        message = f"{malformed} unreadable evidence audit(s) ignored."
    elif resume_available:
        message = "Resume data found."
    elif evidence_present and inspected:
        message = "Collected evidence found; no retryable source recorded."
    elif evidence_present:
        message = "Evidence folder found."
    else:
        message = "Workbook is available for local restoration."
    return RecentAnalysis(
        path=workbook,
        modified_at=datetime.fromtimestamp(stat.st_mtime),
        evidence_present=evidence_present,
        resume_available=resume_available,
        valid=True,
        message=message,
    )
