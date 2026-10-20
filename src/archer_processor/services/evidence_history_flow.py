"""Apply historical display backups after live retry and provider safety gates."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from .evidence_audit import write_evidence_audit
from .evidence_history import EvidenceHistoryError, EvidenceHistoryStore


def apply_history_fallback(
    store: EvidenceHistoryStore | None,
    variants: Iterable[VariantRecord],
    results: dict[str, list[DatabaseEvidence]],
    artifact_root: Path,
    *,
    mtbp_context: str,
    progress: Callable[[str], None],
    checkpoint: Callable[[dict[str, list[DatabaseEvidence]]], None],
) -> dict[str, list[DatabaseEvidence]]:
    if store is None:
        return results
    changed: dict[str, list[DatabaseEvidence]] = {}
    for variant in variants:
        key = f"{variant.sample}|{variant.hgvsc}"
        for index, current in enumerate(results.get(key, [])):
            directory = artifact_root / current.database.lower().replace(" ", "-")
            try:
                fallback = store.fallback(
                    variant, current, directory,
                    context=mtbp_context if current.database == "MTBP" else "",
                )
            except EvidenceHistoryError as exc:
                progress(f"Historical evidence backup unavailable: {exc}")
                continue
            if fallback is current:
                continue
            results[key][index] = fallback
            changed.setdefault(key, []).append(fallback)
            try:
                attempts = current.raw.get("query_attempts", [])
                write_evidence_audit(
                    directory, current.database, variant, fallback,
                    query_attempts=attempts if isinstance(attempts, list) else [],
                )
            except OSError as exc:
                progress(f"Historical result audit could not be saved: {exc}")
            progress(f"{current.database}: Tidligere resultat; nytt oppslag: {current.status}")
    if changed:
        checkpoint(changed)
    return results
