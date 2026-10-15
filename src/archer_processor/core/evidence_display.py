"""Consistent, explicit presentation of previously captured evidence."""

from __future__ import annotations

from datetime import datetime

from .models import DatabaseEvidence


def is_historical(evidence: DatabaseEvidence) -> bool:
    return evidence.status.strip().casefold() == "historical"


def historical_captured_at(evidence: DatabaseEvidence) -> datetime | None:
    """Read the original aware timestamp, never the time a backup was reused."""
    history = evidence.raw.get("historical_result")
    value = history.get("captured_at") if isinstance(history, dict) else None
    value = value or evidence.raw.get("captured_at")
    if not isinstance(value, str):
        return None
    try:
        captured_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return captured_at if captured_at.utcoffset() is not None else None


def historical_timestamp(evidence: DatabaseEvidence) -> str:
    captured_at = historical_captured_at(evidence)
    if captured_at is None:
        return "ukjent tidspunkt"
    local = captured_at.astimezone()
    offset = local.strftime("%z")
    offset = offset[:3] + ":" + offset[3:]
    return f"{local:%d.%m.%Y kl. %H:%M} (UTC{offset})"


def historical_result_label(evidence: DatabaseEvidence) -> str:
    return f"Tidligere resultat · {historical_timestamp(evidence)}"


def evidence_status_label(evidence: DatabaseEvidence) -> str:
    if is_historical(evidence):
        return historical_result_label(evidence)
    return evidence.status.replace("_", " ").title()


def evidence_display_text(evidence: DatabaseEvidence, text: str) -> str:
    """Keep an old classification visibly paired with its original date."""
    if is_historical(evidence):
        return f"{historical_result_label(evidence)}\n{text}".strip()
    return text


def historical_attempt_detail(evidence: DatabaseEvidence) -> str:
    history = evidence.raw.get("historical_result")
    attempt = history.get("current_attempt") if isinstance(history, dict) else None
    if not isinstance(attempt, dict):
        return ""
    status = str(attempt.get("status") or "").replace("_", " ")
    summary = str(attempt.get("summary") or "")
    detail = " · ".join(part for part in (status, summary) if part)
    return f"Nytt oppslag: {detail}" if detail else ""
