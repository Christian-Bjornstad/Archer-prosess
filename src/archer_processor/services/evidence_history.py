"""Local, patient-independent backup of previously verified browser evidence."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Lock
from typing import Any
from uuid import uuid4

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.capture_validation import validate_capture
from archer_processor.services.variant_identity import genomic_identity


class EvidenceHistoryError(RuntimeError):
    """History storage is unavailable; the current lookup can still be used."""


_PROVIDERS = {name.casefold(): name for name in ("ClinVar", "Franklin", "OncoKB", "COSMIC", "MTBP")}
_FALLBACK_STATUSES = frozenset({
    "not_found", "error", "timeout", "transient", "session_lost", "login_required",
    "unauthorized", "layout_changed", "rate_limited", "quota_exhausted", "token_required",
    "deferred", "identity_mismatch", "partial_capture",
})
_CLINICAL_FIELDS = frozenset({
    "assembly_verified", "classification_summary", "classification", "oncogenicity",
    "biological_effect", "gene", "gene_info", "alteration", "functional_relevance",
    "functional_evidence", "actionability_tiers", "biomarkers", "analysis_run_date",
    "pipeline_version", "cancer_type", "database_versions", "research_only",
    "match_basis", "identity_matching_version", "submitted_gene_variant_count",
    "complete_submission", "identity_verification", "license_notice",
})
_CURRENT_MTBP_FIELDS = frozenset({
    "analysis_id", "submitted_query", "query_attempts", "query_attempt_results",
    "remote_report_cleanup", "remote_report_recovery", "recovery", "failure_kind",
    "failure_stage", "stage", "render_diagnostics",
})
_PRIVATE_KEYS = re.compile(r"patient|sample|source.?file|source.?row|analysis.?id|password|credential|secret|token|email|visible.?text|report.?path", re.I)
_DATABASE_LOCKS: dict[str, Any] = {}
_DATABASE_LOCKS_GUARD = Lock()


def _database_lock(path: Path):
    key = os.path.normcase(str(path.resolve()))
    with _DATABASE_LOCKS_GUARD:
        return _DATABASE_LOCKS.setdefault(key, Lock())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_history_directory() -> Path:
    from archer_processor.services.settings import AppSettings

    return AppSettings.config_path().parent / "evidence_history"


def current_attempt(evidence: DatabaseEvidence) -> DatabaseEvidence:
    """Recover the latest failed lookup, including its live MTBP report ownership."""
    if evidence.status != "historical":
        return evidence
    metadata = evidence.raw.get("historical_result")
    saved = metadata.get("current_attempt") if isinstance(metadata, dict) else None
    if not isinstance(saved, dict) or saved.get("database") != evidence.database:
        return evidence
    if saved.get("status") not in _FALLBACK_STATUSES:
        return evidence
    if not isinstance(saved.get("raw", {}), dict) or any(
        not isinstance(saved.get(field, ""), str)
        for field in ("summary", "accession", "clinical_significance", "url")
    ):
        return evidence
    try:
        return DatabaseEvidence(**json.loads(json.dumps(saved)))
    except (TypeError, ValueError):
        return evidence


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _normalized_context(value: str) -> str:
    return " ".join(value.split()).casefold()


def _key(variant: VariantRecord, provider: str, context: str, assembly: str) -> tuple[str, dict[str, Any]] | None:
    assembly = {"grch37": "GRCh37", "grch38": "GRCh38"}.get(assembly.strip().casefold(), "")
    identity = genomic_identity(variant, assembly)
    gene = variant.symbol.strip().upper()
    if (
        not assembly or identity is None or not gene or identity.position <= 0
        or identity.chromosome not in {str(number) for number in range(1, 23)} | {"X", "Y", "M"}
        or not re.fullmatch("[ACGT]+", identity.reference)
        or not re.fullmatch("[ACGT]+", identity.alternate)
        or identity.reference == identity.alternate
    ):
        return None
    cancer_context = _normalized_context(context) if provider == "MTBP" else ""
    if provider == "MTBP" and not cancer_context:
        return None
    requested = asdict(identity)
    encoded = json.dumps([provider, gene, requested, cancer_context], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), requested


def _verified(evidence: DatabaseEvidence, variant: VariantRecord, expected: dict[str, Any], context: str, provider: str) -> bool:
    if evidence.status != "found" or "historical_result" in evidence.raw:
        return False
    raw = evidence.raw
    verification = raw.get("identity_verification")
    accepted = isinstance(verification, dict) and verification.get("accepted") is True
    if accepted:
        for field in ("requested", "returned"):
            reported = verification.get(field)
            if reported is not None and reported != expected:
                return False
    if provider == "MTBP":
        from archer_processor.services.evidence_audit import MTBP_MATCHING_REVISION

        revision = raw.get("identity_matching_version")
        return (
            type(revision) is int and revision >= MTBP_MATCHING_REVISION
            and raw.get("match_basis") in {"protein", "cdna", "genomic_grch37"}
            and expected["assembly"] == "GRCh37"
            and bool(_normalized_context(context))
            and (not raw.get("cancer_type") or _normalized_context(str(raw["cancer_type"])) == _normalized_context(context))
        )
    if provider == "ClinVar":
        return accepted and expected["assembly"] == "GRCh37" and raw.get("assembly_verified") == "GRCh37"
    if provider == "COSMIC":
        identifiers = {match.group().upper() for match in re.finditer(r"\bCOS(?:M|V)\d+\b", variant.cosmic_id, re.I)}
        captures = raw.get("screenshots")
        labels = {str(record.get("label", "")) for record in captures if isinstance(record, dict)} if isinstance(captures, list) else set()
        capture_proof = (
            evidence.accession.upper() in identifiers
            and _timestamp(raw.get("captured_at")) is not None
            and {"COSMIC overview", "COSMIC tissue distribution", "COSMIC samples filtered to lymphoid"} <= labels
        )
        return accepted or capture_proof
    return accepted


def _clinical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clinical_value(item) for key, item in value.items() if not _PRIVATE_KEYS.search(str(key))}
    if isinstance(value, (list, tuple)):
        return [_clinical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _captures(raw: dict[str, Any]) -> list[dict[str, str]]:
    records = raw.get("screenshots")
    if isinstance(records, list):
        if any(not isinstance(record, dict) or not record.get("path") for record in records):
            return []
        return [{"label": str(record.get("label", "Evidence")), "path": str(record["path"])}
                for record in records]
    if raw.get("screenshot"):
        return [{"label": "Evidence", "path": str(raw["screenshot"])}]
    return []


def _asset(source: Path, directory: Path, *, expected_hash: str = "") -> tuple[Path, str]:
    """Publish a validated snapshot atomically without mutating an existing asset."""
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if expected_hash and digest != expected_hash:
        raise ValueError("History capture checksum does not match.")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}.png"
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest or not validate_capture(destination).valid:
            raise ValueError("Existing history capture is damaged.")
        return destination, digest
    temporary = directory / f".{digest}-{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(content)
        if not validate_capture(temporary).valid:
            raise ValueError("History capture is incomplete.")
        try:
            os.link(temporary, destination)
        except FileExistsError:
            # Another worker published the same validated, content-addressed asset.
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError("Existing history capture is damaged.")
    finally:
        temporary.unlink(missing_ok=True)
    return destination, digest


class EvidenceHistoryStore:
    def __init__(self, directory: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self.directory = Path(directory)
        self.database_path = self.directory / "history.sqlite3"
        self._clock = clock

    @contextmanager
    def _connect(self, *, write: bool):
        # WAL's initial mode transition can raise SQLITE_BUSY without waiting.
        # Share the gate across store instances, covering initialization and the
        # transaction; immutable capture files can still be prepared in parallel.
        with _database_lock(self.database_path):
            connection = None
            try:
                if write:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    connection = sqlite3.connect(self.database_path, timeout=10)
                else:
                    connection = sqlite3.connect(f"{self.database_path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
                connection.execute("PRAGMA busy_timeout=10000")
                if write:
                    if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                        connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("""CREATE TABLE IF NOT EXISTS results (
                        identity_key TEXT NOT NULL, content_hash TEXT NOT NULL,
                        captured_at REAL NOT NULL, payload TEXT NOT NULL,
                        PRIMARY KEY (identity_key, content_hash)
                    )""")
                    connection.execute("CREATE INDEX IF NOT EXISTS results_latest ON results(identity_key, captured_at DESC)")
                with connection:
                    yield connection
            except (OSError, sqlite3.Error) as exc:
                raise EvidenceHistoryError("Local evidence history could not be accessed.") from exc
            finally:
                if connection is not None:
                    connection.close()

    def record(self, variant: VariantRecord, evidence: DatabaseEvidence, *, context: str = "",
               captured_at: datetime | None = None, assembly: str = "GRCh37") -> bool:
        provider = _PROVIDERS.get(evidence.database.casefold())
        identity = _key(variant, provider or "", context, assembly) if provider else None
        if identity is None or not _verified(evidence, variant, identity[1], context, provider):
            return False
        original_time = _timestamp(captured_at if captured_at is not None else evidence.raw.get("captured_at", self._clock()))
        captures = _captures(evidence.raw)
        if original_time is None or not captures or any(not validate_capture(Path(record["path"])).valid for record in captures):
            return False
        full_report = evidence.raw.get("patient_report_screenshot")
        if full_report and any(Path(record["path"]).resolve() == Path(str(full_report)).resolve() for record in captures):
            return False
        try:
            saved_captures = []
            for record in captures:
                asset, digest = _asset(Path(record["path"]), self.directory / "assets")
                saved_captures.append({"label": record["label"], "asset": asset.name, "sha256": digest})
            raw = {field: _clinical_value(value) for field, value in evidence.raw.items() if field in _CLINICAL_FIELDS}
            payload = {
                "database": provider, "summary": evidence.summary,
                "accession": "" if provider == "MTBP" else evidence.accession,
                "clinical_significance": evidence.clinical_significance,
                "raw": raw, "captures": saved_captures,
                "captured_at": original_time.isoformat(),
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            with self._connect(write=True) as connection:
                connection.execute("INSERT OR IGNORE INTO results VALUES (?, ?, ?, ?)",
                                   (identity[0], fingerprint, original_time.timestamp(), encoded))
        except (OSError, ValueError, TypeError) as exc:
            raise EvidenceHistoryError("Verified evidence could not be saved to local history.") from exc
        return True

    def fallback(self, variant: VariantRecord, current: DatabaseEvidence, artifact_directory: Path, *,
                 context: str = "", assembly: str = "GRCh37") -> DatabaseEvidence:
        provider = _PROVIDERS.get(current.database.casefold())
        identity = _key(variant, provider or "", context, assembly) if provider else None
        if current.status not in _FALLBACK_STATUSES or identity is None or not self.database_path.exists():
            return current
        try:
            with self._connect(write=False) as connection:
                rows = connection.execute("SELECT captured_at, payload FROM results WHERE identity_key=? ORDER BY captured_at DESC, content_hash", (identity[0],)).fetchall()
            for captured_seconds, encoded in rows:
                try:
                    payload = json.loads(encoded)
                    captures = payload["captures"]
                    if not captures or payload["database"] != provider:
                        continue
                    # Reject invalid/missing assets before publishing any output captures.
                    assets = []
                    for record in captures:
                        digest = record["sha256"]
                        if not re.fullmatch(r"[0-9a-f]{64}", digest) or record["asset"] != f"{digest}.png":
                            raise ValueError("Invalid history asset reference.")
                        source = self.directory / "assets" / record["asset"]
                        if not validate_capture(source).valid or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                            raise ValueError("History capture is unavailable.")
                        assets.append((record, source))
                    try:
                        copied = [{"label": record["label"], "path": str(_asset(source, Path(artifact_directory), expected_hash=record["sha256"])[0]), "url": ""}
                                  for record, source in assets]
                    except (OSError, ValueError) as exc:
                        raise EvidenceHistoryError("Previous captures could not be copied into the current evidence folder.") from exc
                    captured = datetime.fromtimestamp(captured_seconds, timezone.utc).isoformat()
                    used = _timestamp(self._clock())
                    if used is None:
                        raise EvidenceHistoryError("History clock did not provide a timezone-aware timestamp.")
                    raw = {**payload["raw"], "captured_at": captured, "screenshots": copied,
                           "screenshot": copied[0]["path"], "historical_result": {
                               "captured_at": captured, "used_at": used.isoformat(), "current_attempt": asdict(current),
                           }}
                    if provider == "MTBP":
                        raw.update({field: json.loads(json.dumps(value)) for field, value in current.raw.items() if field in _CURRENT_MTBP_FIELDS})
                    return DatabaseEvidence(provider, "historical", payload["summary"], accession=payload["accession"],
                                            clinical_significance=payload["clinical_significance"], raw=raw)
                except (KeyError, TypeError, ValueError, OSError, OverflowError):
                    continue
        except (OSError, ValueError, TypeError) as exc:
            raise EvidenceHistoryError("Previous evidence could not be read from local history.") from exc
        return current
