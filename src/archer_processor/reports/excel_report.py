from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from archer_processor.core.highlights import variant_highlight
from archer_processor.core.models import DatabaseEvidence, ProcessingResult, VariantRecord


DEFAULT_DATABASE_COLUMNS = [
    "ClinVar",
    "gnomAD",
    "COSMIC",
    "CIViC",
    "CancerMine",
    "DGIdb",
    "ClinGen Allele Registry",
    "cBioPortal",
    "OncoKB",
    "Franklin",
    "MTBP",
    "HSMD",
]


class ExcelReportWriter:
    colors = {
        "navy": "163B5C",
        "blue": "2F75B5",
        "pale_blue": "D9EAF7",
        "green": "E2F0D9",
        "yellow": "FFF2CC",
        "orange": "FCE4D6",
        "red": "C00000",
        "red_text": "FFFFFF",
        "gray": "F2F2F2",
        "border": "B7C9D6",
        "white": "FFFFFF",
    }

    def write(
        self,
        result: ProcessingResult,
        output_path: Path,
        evidence: dict[str, list[DatabaseEvidence]] | None = None,
        hide_excluded: bool = True,
    ) -> Path:
        workbook = Workbook()
        workbook.remove(workbook.active)
        self._summary_sheet(workbook, result)
        self._variant_sheet(workbook, "Variants", result.variants, result.run_date, evidence or {}, hide_excluded)
        self._history_sheet(workbook, result.variants)
        self._database_sheet(workbook, result.variants, evidence or {})
        self._rules_sheet(workbook, result)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(output_path)
        return output_path

    def _summary_sheet(self, workbook: Workbook, result: ProcessingResult) -> None:
        ws = workbook.create_sheet("Summary")
        ws["A1"] = "Archer VPM Processing Summary"
        ws["A1"].font = Font(size=16, bold=True, color=self.colors["navy"])
        rows = [
            ("Input", str(result.input_path)),
            ("Run date", result.run_date),
            ("Total variants", result.total_count),
            ("Included", len(result.included)),
            ("Excluded", len(result.excluded)),
            ("Flagged", len(result.flagged)),
            ("Processing time", f"{result.duration_seconds:.2f} s"),
        ]
        for row_index, (label, value) in enumerate(rows, start=3):
            ws.cell(row_index, 1, label).font = Font(bold=True)
            ws.cell(row_index, 2, value)
        self._fit(ws)

    def _variant_sheet(
        self,
        workbook: Workbook,
        title: str,
        variants: list[VariantRecord],
        run_date: str,
        evidence: dict[str, list[DatabaseEvidence]],
        hide_excluded: bool,
    ) -> None:
        ws = workbook.create_sheet(title)
        database_columns = self._database_columns(evidence)
        headers = [
            "Sample",
            "Patient",
            "Decision",
            "Reason",
            "Gene",
            "HGVSc",
            "HGVSp",
            "Transcript",
            "AF",
            "Depth",
            "AO",
            "Type",
            "Quality",
            "Consequence",
            "Genomic Location",
            "Ref",
            "Alt",
            "Classification",
            "Report",
            "Artifact",
            "History",
            "Warnings",
            *[f"{database} Evidence" for database in database_columns],
            "Run Date",
        ]
        self._headers(ws, headers)
        for row_index, variant in enumerate(variants, start=2):
            evidence_by_database = self._evidence_by_database(evidence.get(self._key(variant), []))
            values = [
                variant.sample,
                variant.patient_id,
                variant.decision,
                variant.decision_reason,
                variant.symbol,
                variant.hgvsc,
                variant.hgvsp,
                variant.transcript,
                variant.af,
                variant.depth,
                variant.ao,
                variant.variant_type,
                variant.quality_score,
                variant.consequence,
                variant.genomic_location,
                variant.ref_allele,
                variant.alt_allele,
                variant.classification,
                variant.report_status,
                variant.artifact_status,
                f"{len(variant.history_matches)} previous match(es)",
                "; ".join(variant.warnings),
                *[self._evidence_cell(evidence_by_database.get(database, [])) for database in database_columns],
                run_date,
            ]
            evidence_start = headers.index(f"{database_columns[0]} Evidence") + 1 if database_columns else 0
            evidence_columns = set(range(evidence_start, evidence_start + len(database_columns)))
            for col_index, value in enumerate(values, start=1):
                cell = ws.cell(row_index, col_index, value)
                cell.border = self._border()
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=col_index in {4, 14, 21, 22} or col_index in evidence_columns,
                )
                if col_index == 9 and value is not None:
                    cell.number_format = "0.00%"
            self._style_variant_row(ws, row_index, variant)
            if hide_excluded and variant.decision == "excluded":
                ws.row_dimensions[row_index].hidden = True
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(variants) + 1}"
        self._fit(ws, max_width=46)

    def _history_sheet(self, workbook: Workbook, variants: list[VariantRecord]) -> None:
        ws = workbook.create_sheet("Local History")
        headers = ["Sample", "Gene", "HGVSc", "Previous Sample", "Previous Classification", "TO", "Rept", "Germ", "Artf"]
        self._headers(ws, headers)
        row_index = 2
        for variant in variants:
            for match in variant.history_matches:
                values = [
                    variant.sample,
                    variant.symbol,
                    variant.hgvsc,
                    match.get("Sample"),
                    match.get("Classification"),
                    match.get("TO"),
                    match.get("Rept"),
                    match.get("Germ"),
                    match.get("Artf"),
                ]
                for col_index, value in enumerate(values, start=1):
                    ws.cell(row_index, col_index, value).border = self._border()
                row_index += 1
        self._fit(ws)

    def _database_sheet(
        self,
        workbook: Workbook,
        variants: list[VariantRecord],
        evidence: dict[str, list[DatabaseEvidence]],
    ) -> None:
        ws = workbook.create_sheet("Database Evidence")
        headers = ["Sample", "Gene", "HGVSc", "Database", "Status", "Significance", "Accession", "Summary", "URL"]
        self._headers(ws, headers)
        row_index = 2
        for variant in variants:
            for item in evidence.get(self._key(variant), []):
                values = [
                    variant.sample,
                    variant.symbol,
                    variant.hgvsc,
                    item.database,
                    item.status,
                    item.clinical_significance,
                    item.accession,
                    item.summary,
                    item.url,
                ]
                for col_index, value in enumerate(values, start=1):
                    cell = ws.cell(row_index, col_index, value)
                    cell.border = self._border()
                    cell.alignment = Alignment(vertical="top", wrap_text=col_index in {8, 9})
                row_index += 1
        self._fit(ws, max_width=60)

    def _rules_sheet(self, workbook: Workbook, result: ProcessingResult) -> None:
        ws = workbook.create_sheet("Rules")
        self._headers(ws, ["Rule ID"])
        for row_index, rule in enumerate(result.rules_applied, start=2):
            ws.cell(row_index, 1, rule)
        self._fit(ws)

    def _style_variant_row(self, ws, row_index: int, variant: VariantRecord) -> None:
        fill = {
            "artifact": self.colors["orange"],
            "tier": self.colors["yellow"],
            "germline": self.colors["green"],
        }.get(variant_highlight(variant))
        if fill:
            for cell in ws[row_index]:
                cell.fill = PatternFill("solid", fgColor=fill)

    def _headers(self, ws, headers: list[str]) -> None:
        for col_index, header in enumerate(headers, start=1):
            cell = ws.cell(1, col_index, header)
            cell.fill = PatternFill("solid", fgColor=self.colors["navy"])
            cell.font = Font(bold=True, color=self.colors["white"])
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = self._border()

    def _fit(self, ws, max_width: int = 38) -> None:
        for column_cells in ws.columns:
            width = max(len(str(cell.value or "")) for cell in column_cells) + 2
            ws.column_dimensions[column_cells[0].column_letter].width = min(max(width, 10), max_width)

    def _border(self) -> Border:
        side = Side(style="thin", color=self.colors["border"])
        return Border(left=side, right=side, top=side, bottom=side)

    def _key(self, variant: VariantRecord) -> str:
        return f"{variant.sample}|{variant.hgvsc}"

    def _database_columns(self, evidence: dict[str, list[DatabaseEvidence]]) -> list[str]:
        seen = {
            item.database
            for items in evidence.values()
            for item in items
            if item.database
        }
        return DEFAULT_DATABASE_COLUMNS + sorted(seen.difference(DEFAULT_DATABASE_COLUMNS))

    def _evidence_by_database(self, evidence_items: list[DatabaseEvidence]) -> dict[str, list[DatabaseEvidence]]:
        grouped: dict[str, list[DatabaseEvidence]] = defaultdict(list)
        for item in evidence_items:
            grouped[item.database].append(item)
        return grouped

    def _evidence_cell(self, evidence_items: list[DatabaseEvidence]) -> str:
        return "\n".join(
            f"[{item.status}] {item.summary}".strip()
            for item in evidence_items
        )
