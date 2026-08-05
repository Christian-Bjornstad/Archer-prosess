from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from archer_processor.core.models import VariantRecord


class ArcherTsvReader:
    required_columns = {
        "Sample",
        "Symbol",
        "HGVSc",
        "AF",
        "Depth",
        "AO",
        "Type",
        "Source",
        "Genomic Location",
        "Ref/Alt Allele",
    }

    def validate(self, path: Path) -> tuple[bool, list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        if not path.exists():
            return False, [f"File not found: {path}"], warnings
        if path.suffix.lower() not in {".tsv", ".txt"}:
            warnings.append("File extension is not .tsv or .txt; trying tab-delimited parsing.")
        try:
            columns = list(pd.read_csv(path, sep="\t", nrows=0).columns)
        except Exception as exc:
            return False, [f"Could not read TSV header: {exc}"], warnings
        missing = sorted(self.required_columns - set(columns))
        if missing:
            errors.append("Missing required columns: " + ", ".join(missing))
        duplicates = sorted({column for column in columns if columns.count(column) > 1})
        if duplicates:
            warnings.append("Duplicate columns found: " + ", ".join(duplicates))
        return not errors, errors, warnings

    def read(self, path: Path) -> list[VariantRecord]:
        ok, errors, _warnings = self.validate(path)
        if not ok:
            raise ValueError("; ".join(errors))
        frame = pd.read_csv(path, sep="\t", low_memory=False)
        return [
            self.row_to_variant(path, index + 2, row.to_dict())
            for index, row in frame.iterrows()
        ]

    def row_to_variant(
        self, path: Path, source_row: int, row: dict[str, Any]
    ) -> VariantRecord:
        return self._row_to_variant(path, source_row - 2, row)

    def _row_to_variant(self, path: Path, index: int, row: dict[str, Any]) -> VariantRecord:
        ref, alt = self._parse_ref_alt(row.get("Ref/Alt Allele"))
        return VariantRecord(
            source_file=path,
            source_row=index + 2,
            sample=self._text(row.get("Sample")),
            symbol=self._text(row.get("Symbol")),
            hgvsc=self._text(row.get("HGVSc")),
            hgvsp=self._text(row.get("HGVSp")),
            transcript=self._text(row.get("Trans")),
            genomic_location=self._text(row.get("Genomic Location")),
            ref_allele=ref,
            alt_allele=alt,
            variant_type=self._text(row.get("Type")),
            depth=self._int(row.get("Depth")),
            ao=self._int(row.get("AO")),
            af=self._float(row.get("AF")),
            quality_score=self._float(row.get("Quality Score")),
            gnomad_af=self._float(row.get("gnomAD AF")),
            consequence=self._text(row.get("Consequence")),
            clinical_significance=self._text(row.get("Clinical Significance")),
            cosmic_id=self._text(row.get("COSMICID")),
            dbsnp_id=self._text(row.get("DBSNPID")),
            source_caller=self._text(row.get("Source")),
            classification=self._text(row.get("Classification")),
            report_status=self._text(row.get("Report")),
            artifact_status=self._text(row.get("Artifact")),
            has_sample_strand_bias=self._text(row.get("Has Sample Strand Bias")),
            has_seq_dir_bias=self._text(row.get("Has Seq Dir Bias")),
            af_outlier_p_value=self._float(row.get("AF Outlier P Value")),
            raw=row,
        )

    def _parse_ref_alt(self, value: Any) -> tuple[str, str]:
        text = self._text(value)
        if "/" not in text:
            return "", ""
        ref, alt = text.split("/", 1)
        return ref.strip(), alt.strip()

    def _text(self, value: Any) -> str:
        if value is None or pd.isna(value):
            return ""
        return str(value).strip()

    def _float(self, value: Any) -> float | None:
        if value is None or pd.isna(value) or value == "":
            return None
        try:
            return float(str(value).replace(",", "."))
        except ValueError:
            return None

    def _int(self, value: Any) -> int | None:
        number = self._float(value)
        return int(number) if number is not None else None
