from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class VariantRecord:
    source_file: Path
    source_row: int
    sample: str
    symbol: str
    hgvsc: str
    hgvsp: str = ""
    transcript: str = ""
    genomic_location: str = ""
    ref_allele: str = ""
    alt_allele: str = ""
    variant_type: str = ""
    depth: int | None = None
    ao: int | None = None
    af: float | None = None
    quality_score: float | None = None
    gnomad_af: float | None = None
    consequence: str = ""
    clinical_significance: str = ""
    cosmic_id: str = ""
    dbsnp_id: str = ""
    source_caller: str = ""
    classification: str = ""
    report_status: str = ""
    artifact_status: str = ""
    has_sample_strand_bias: str = ""
    has_seq_dir_bias: str = ""
    af_outlier_p_value: float | None = None
    decision: str = "included"
    decision_reason: str = ""
    matched_rules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    history_matches: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def patient_id(self) -> str:
        return self.sample.split("_VPM_")[0] if "_VPM_" in self.sample else self.sample

    @property
    def display_name(self) -> str:
        return f"{self.symbol} {self.hgvsc}".strip()


@dataclass(frozen=True, slots=True)
class FilterRule:
    rule_id: str
    name: str
    reason: str
    hgvsc: str
    action: str = "excluded"
    gene: str = ""
    max_af_exclusive: float | None = None


@dataclass(slots=True)
class DatabaseEvidence:
    database: str
    status: str
    summary: str = ""
    accession: str = ""
    clinical_significance: str = ""
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProcessingResult:
    input_path: Path
    output_path: Path | None
    run_date: str
    variants: list[VariantRecord]
    rules_applied: list[str]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None

    @property
    def included(self) -> list[VariantRecord]:
        return [variant for variant in self.variants if variant.decision == "included"]

    @property
    def excluded(self) -> list[VariantRecord]:
        return [variant for variant in self.variants if variant.decision == "excluded"]

    @property
    def flagged(self) -> list[VariantRecord]:
        return [variant for variant in self.variants if variant.decision == "flagged"]

    @property
    def total_count(self) -> int:
        return len(self.variants)

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or datetime.now()
        return (end - self.started_at).total_seconds()
