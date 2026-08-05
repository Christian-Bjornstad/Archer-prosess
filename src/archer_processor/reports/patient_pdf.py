from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image as ReportImage,
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from archer_processor.core.models import DatabaseEvidence, ProcessingResult, VariantRecord


SCREENSHOT_DATABASE_ORDER = ("MTBP", "Franklin", "ClinVar", "OncoKB", "COSMIC")


DIT_PATTERN = re.compile(r"^\d{2}OUM\d{5}$", flags=re.IGNORECASE)


class PatientPdfReportWriter:
    """Create one decision-support PDF for each DIT with included variants."""

    navy = colors.HexColor("#163B5C")
    blue = colors.HexColor("#2F75B5")
    pale_blue = colors.HexColor("#EAF3FA")
    pale_green = colors.HexColor("#EAF5ED")
    pale_orange = colors.HexColor("#FCE4D6")
    pale_gray = colors.HexColor("#F2F2F2")
    border = colors.HexColor("#B7C9D6")
    ink = colors.HexColor("#17212B")
    muted = colors.HexColor("#5E6A73")

    review_statuses = {
        "ambiguous_result",
        "error",
        "identity_mismatch",
        "invalid_query",
        "login_required",
        "manual",
        "quota_exhausted",
        "timeout",
        "token_required",
        "unauthorized",
    }

    def __init__(self) -> None:
        base = getSampleStyleSheet()
        self.styles = {
            "title": ParagraphStyle(
                "PatientReportTitle",
                parent=base["Title"],
                fontName="Helvetica-Bold",
                fontSize=20,
                leading=24,
                textColor=colors.white,
                alignment=TA_LEFT,
                spaceAfter=0,
            ),
            "subtitle": ParagraphStyle(
                "PatientReportSubtitle",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=9,
                leading=12,
                textColor=self.muted,
                spaceAfter=8,
            ),
            "h1": ParagraphStyle(
                "PatientReportH1",
                parent=base["Heading1"],
                fontName="Helvetica-Bold",
                fontSize=14,
                leading=17,
                textColor=self.navy,
                spaceBefore=10,
                spaceAfter=6,
            ),
            "h2": ParagraphStyle(
                "PatientReportH2",
                parent=base["Heading2"],
                fontName="Helvetica-Bold",
                fontSize=11,
                leading=14,
                textColor=self.navy,
                spaceBefore=8,
                spaceAfter=5,
            ),
            "body": ParagraphStyle(
                "PatientReportBody",
                parent=base["BodyText"],
                fontName="Helvetica",
                fontSize=8.5,
                leading=11.5,
                textColor=self.ink,
                spaceAfter=5,
            ),
            "small": ParagraphStyle(
                "PatientReportSmall",
                parent=base["BodyText"],
                fontName="Helvetica",
                fontSize=7.5,
                leading=10,
                textColor=self.ink,
            ),
            "label": ParagraphStyle(
                "PatientReportLabel",
                parent=base["BodyText"],
                fontName="Helvetica-Bold",
                fontSize=8,
                leading=10,
                textColor=self.navy,
            ),
            "table_header": ParagraphStyle(
                "PatientReportTableHeader",
                parent=base["BodyText"],
                fontName="Helvetica-Bold",
                fontSize=7.5,
                leading=9.5,
                textColor=colors.white,
            ),
            "callout": ParagraphStyle(
                "PatientReportCallout",
                parent=base["BodyText"],
                fontName="Helvetica-Bold",
                fontSize=8.5,
                leading=12,
                textColor=self.ink,
                alignment=TA_LEFT,
            ),
            "center": ParagraphStyle(
                "PatientReportCenter",
                parent=base["BodyText"],
                fontName="Helvetica-Bold",
                fontSize=8.5,
                leading=11,
                textColor=self.navy,
                alignment=TA_CENTER,
            ),
        }

    def write_all(
        self,
        result: ProcessingResult,
        output_directory: Path,
        evidence: dict[str, list[DatabaseEvidence]] | None = None,
    ) -> list[Path]:
        evidence = evidence or {}
        grouped: dict[str, list[VariantRecord]] = defaultdict(list)
        for variant in result.variants:
            grouped[variant.patient_id].append(variant)

        output_directory.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        for patient_id, patient_variants in sorted(grouped.items()):
            included = [
                variant for variant in patient_variants
                if variant.decision == "included"
            ]
            if not included:
                continue
            safe_patient_id = re.sub(r"[^A-Za-z0-9_-]+", "_", patient_id).strip("_")
            safe_date = re.sub(r"[^0-9-]+", "", result.run_date) or "undated"
            output_path = output_directory / (
                f"{safe_patient_id}_variant_review_{safe_date}.pdf"
            )
            self.write_patient(
                patient_id,
                patient_variants,
                included,
                result,
                output_path,
                evidence,
            )
            outputs.append(output_path)
        return outputs

    def write_patient(
        self,
        patient_id: str,
        patient_variants: list[VariantRecord],
        included_variants: list[VariantRecord],
        result: ProcessingResult,
        output_path: Path,
        evidence: dict[str, list[DatabaseEvidence]],
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=A4,
            rightMargin=1.5 * cm,
            leftMargin=1.5 * cm,
            topMargin=1.35 * cm,
            bottomMargin=1.55 * cm,
            title=f"Variant review - {patient_id}",
            author="Archer Prosess",
            subject="Patient-level variant evidence review",
        )
        story: list[Any] = []
        story.extend(self._header(patient_id, result.run_date))

        dit_message = self._dit_validation_message(patient_id, result.run_date)
        if dit_message:
            story.append(self._callout(dit_message, self.pale_orange))
            story.append(Spacer(1, 0.12 * cm))

        patient_evidence = [
            item
            for variant in included_variants
            for item in evidence.get(self._key(variant), [])
        ]
        sources = list(dict.fromkeys(item.database for item in patient_evidence))
        pending = [
            item for item in patient_evidence
            if item.status in self.review_statuses
        ]
        summary_data = [
            [self._p("Included variants", "label"), self._p(str(len(included_variants)), "center")],
            [self._p("Filtered variants", "label"), self._p(str(len(patient_variants) - len(included_variants)), "center")],
            [self._p("Evidence records", "label"), self._p(str(len(patient_evidence)), "center")],
            [self._p("Items requiring follow-up", "label"), self._p(str(len(pending)), "center")],
            [self._p("Sources represented", "label"), self._p(", ".join(sources) or "None captured", "small")],
            [self._p("Sample/run identifiers", "label"), self._p(", ".join(dict.fromkeys(v.sample for v in patient_variants)), "small")],
        ]
        summary = Table(summary_data, colWidths=[4.6 * cm, 12.1 * cm])
        summary.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), self.pale_blue),
                    ("GRID", (0, 0), (-1, -1), 0.45, self.border),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(summary)
        story.append(Spacer(1, 0.18 * cm))
        story.append(
            self._callout(
                "Decision-support report. Evidence must be checked against the linked "
                "provider record and interpreted by qualified clinical personnel in "
                "the correct diagnosis, tumor type, specimen, and treatment context.",
                self.pale_orange,
            )
        )

        story.append(self._p("Included variant review", "h1"))
        for index, variant in enumerate(included_variants, start=1):
            story.extend(self._variant_section(index, variant, evidence.get(self._key(variant), [])))

        story.extend(self._review_section(included_variants, patient_evidence))
        story.extend(self._conclusion_section())
        story.extend(self._limitations_section(result, patient_evidence))
        story.extend(self._image_appendix(included_variants, evidence))

        footer = self._footer(patient_id, result.run_date)
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        return output_path

    def _header(self, patient_id: str, run_date: str) -> list[Any]:
        title = Table(
            [[self._p("Patient Variant Evidence Review", "title")]],
            colWidths=[17.1 * cm],
        )
        title.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), self.navy),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ]
            )
        )
        identity = Table(
            [
                [self._p("DIT identifier", "label"), self._p(patient_id, "body"),
                 self._p("Report date", "label"), self._p(run_date, "body")],
            ],
            colWidths=[2.5 * cm, 6.0 * cm, 2.5 * cm, 6.1 * cm],
        )
        identity.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), self.pale_blue),
                    ("BOX", (0, 0), (-1, -1), 0.5, self.border),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        return [title, Spacer(1, 0.12 * cm), identity, Spacer(1, 0.18 * cm)]

    def _variant_section(
        self,
        index: int,
        variant: VariantRecord,
        evidence_items: list[DatabaseEvidence],
    ) -> list[Any]:
        heading = f"{index}. {variant.symbol} - {variant.hgvsc or variant.genomic_location}"
        details = [
            [self._p("Gene", "label"), self._p(variant.symbol, "small"),
             self._p("Decision", "label"), self._p(variant.decision.title(), "small")],
            [self._p("HGVSc", "label"), self._p(variant.hgvsc or "Not available", "small"),
             self._p("HGVSp", "label"), self._p(variant.hgvsp or "Not available", "small")],
            [self._p("Transcript", "label"), self._p(variant.transcript or "Not available", "small"),
             self._p("Consequence", "label"), self._p(variant.consequence or "Not available", "small")],
            [self._p("Genomic", "label"), self._p(variant.genomic_location or "Not available", "small"),
             self._p("Ref / Alt", "label"), self._p(self._alleles(variant), "small")],
            [self._p("AF / gnomAD AF", "label"), self._p(self._af_values(variant), "small"),
             self._p("Depth / AO", "label"), self._p(self._depth_ao(variant), "small")],
            [self._p("Quality / caller", "label"), self._p(self._quality_caller(variant), "small"),
             self._p("Known IDs", "label"), self._p(self._known_ids(variant), "small")],
            [self._p("Archer classification", "label"), self._p(variant.classification or variant.clinical_significance or "Not provided", "small"),
             self._p("Local history", "label"), self._p(f"{len(variant.history_matches)} previous match(es)", "small")],
        ]
        detail_table = Table(details, colWidths=[2.6 * cm, 5.9 * cm, 2.6 * cm, 6.0 * cm])
        detail_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), self.pale_blue),
                    ("BACKGROUND", (2, 0), (2, -1), self.pale_blue),
                    ("GRID", (0, 0), (-1, -1), 0.4, self.border),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )

        lead: list[Any] = [self._p(heading, "h2"), detail_table]
        return [
            KeepTogether(lead),
            Spacer(1, 0.1 * cm),
            self._p("Database evidence", "label"),
            self._evidence_table(evidence_items),
            Spacer(1, 0.16 * cm),
        ]

    def _evidence_table(self, evidence_items: list[DatabaseEvidence]) -> Any:
        if not evidence_items:
            return self._callout(
                "No database evidence has been captured for this variant.",
                self.pale_gray,
            )
        data = [[
            self._p("Source", "table_header"),
            self._p("Status", "table_header"),
            self._p("Classification / significance", "table_header"),
            self._p("Evidence summary", "table_header"),
            self._p("Links", "table_header"),
        ]]
        for item in evidence_items:
            links = []
            if item.url:
                links.append(
                    f"<link href={quoteattr(item.url)}><font color='#2F75B5'><u>Source</u></font></link>"
                )
            screenshot = str(item.raw.get("screenshot") or "")
            if screenshot and Path(screenshot).exists():
                links.append(
                    f"<link href={quoteattr(Path(screenshot).resolve().as_uri())}>"
                    "<font color='#2F75B5'><u>Screenshot</u></font></link>"
                )
            data.append(
                [
                    self._p(item.database, "small"),
                    self._p(item.status.replace("_", " ").title(), "small"),
                    self._p(self._significance(item), "small"),
                    self._p(self._evidence_summary(item), "small"),
                    Paragraph("<br/>".join(links) or "-", self.styles["small"]),
                ]
            )
        table = LongTable(
            data,
            colWidths=[2.25 * cm, 2.35 * cm, 3.15 * cm, 7.1 * cm, 2.25 * cm],
            repeatRows=1,
        )
        commands = [
            ("BACKGROUND", (0, 0), (-1, 0), self.navy),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, self.border),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for row_index, item in enumerate(evidence_items, start=1):
            fill = self.pale_green if item.status == "found" else self.pale_gray
            if item.status in self.review_statuses:
                fill = self.pale_orange
            commands.append(("BACKGROUND", (1, row_index), (1, row_index), fill))
        table.setStyle(TableStyle(commands))
        return table

    def _review_section(
        self,
        variants: list[VariantRecord],
        evidence_items: list[DatabaseEvidence],
    ) -> list[Any]:
        items = [
            "Confirm transcript, genome build, and variant identity against the laboratory source file.",
            "Review every linked provider record in the relevant disease and specimen context.",
            "Resolve login, timeout, ambiguity, missing-result, and conflicting-classification items before sign-off.",
            "Document the clinical interpretation and action separately from database classifications.",
        ]
        pending = [item for item in evidence_items if item.status in self.review_statuses]
        classification_significances = {
            item.clinical_significance.strip().casefold()
            for item in evidence_items
            if item.database in {"ClinVar", "Franklin", "HSMD"}
            and item.clinical_significance.strip()
        }
        if pending:
            items.append(
                f"This report contains {len(pending)} evidence record(s) requiring follow-up."
            )
        if len(classification_significances) > 1:
            items.append(
                "Different classifications/significance labels are present across sources and require reconciliation."
            )
        story: list[Any] = [self._p("Clinical review checklist", "h1")]
        for item in items:
            story.append(Paragraph(escape(item), self.styles["body"], bulletText="-"))
        return story

    def _conclusion_section(self) -> list[Any]:
        rows = [[self._p("Responsible physician conclusion", "label")]]
        rows.extend([""] for _ in range(4))
        box = Table(rows, colWidths=[17.1 * cm], rowHeights=[0.65 * cm] + [0.8 * cm] * 4)
        box.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), self.pale_blue),
                    ("BOX", (0, 0), (-1, -1), 0.7, self.navy),
                    ("LINEBELOW", (0, 1), (-1, -2), 0.3, self.border),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        signature = Table(
            [
                [self._p("Name / role", "label"), "", self._p("Date", "label"), ""],
                [self._p("Signature", "label"), "", "", ""],
            ],
            colWidths=[2.5 * cm, 7.0 * cm, 1.5 * cm, 6.1 * cm],
            rowHeights=[0.75 * cm, 0.85 * cm],
        )
        signature.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.5, self.border),
                    ("GRID", (0, 0), (-1, -1), 0.35, self.border),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return [
            KeepTogether(
                [
                    self._p("Conclusion and sign-off", "h1"),
                    box,
                    Spacer(1, 0.18 * cm),
                    signature,
                ]
            )
        ]

    def _limitations_section(
        self,
        result: ProcessingResult,
        evidence_items: list[DatabaseEvidence],
    ) -> list[Any]:
        limitations = [
            "This document summarizes computational and database evidence; it does not establish a diagnosis or treatment recommendation.",
            "Database content can change after the capture date. Re-open linked sources when making a clinical decision.",
            "The DIT is a pseudonymous identifier. Demographics, diagnosis, specimen quality, tumor fraction, and treatment history are not included unless supplied elsewhere.",
            f"The report was generated from {result.input_path.name} for run date {result.run_date}.",
        ]
        if any(item.database == "MTBP" for item in evidence_items):
            limitations.append(
                "MTBP public portal output is marked for academic research use only and must not be treated as standalone clinical reporting evidence."
            )
        if any(item.database == "COSMIC" for item in evidence_items):
            limitations.append(
                "COSMIC content is licence-controlled. Confirm that the organisation's "
                "COSMIC licence permits use in patient-care reporting before clinical use."
            )
        if any(item.raw.get("screenshot") for item in evidence_items):
            limitations.append(
                "Provider screenshots are embedded in the evidence image appendix. "
                "They remain point-in-time captures and must be checked against the live source."
            )
        story: list[Any] = [self._p("Limitations and provenance", "h1")]
        for limitation in limitations:
            story.append(Paragraph(escape(limitation), self.styles["body"], bulletText="-"))
        return story

    def _image_appendix(
        self,
        variants: list[VariantRecord],
        evidence: dict[str, list[DatabaseEvidence]],
    ) -> list[Any]:
        entries: list[tuple[VariantRecord, DatabaseEvidence, dict[str, str]]] = []
        seen: set[str] = set()
        for variant in variants:
            items = evidence.get(self._key(variant), [])
            ordered_items = sorted(
                items,
                key=lambda item: (
                    SCREENSHOT_DATABASE_ORDER.index(item.database)
                    if item.database in SCREENSHOT_DATABASE_ORDER
                    else len(SCREENSHOT_DATABASE_ORDER)
                ),
            )
            for item in ordered_items:
                for record in self._screenshot_records(item):
                    screenshot_path = Path(record["path"])
                    try:
                        identity = str(screenshot_path.resolve())
                    except OSError:
                        identity = str(screenshot_path)
                    if identity in seen or not screenshot_path.is_file():
                        continue
                    try:
                        ImageReader(str(screenshot_path)).getSize()
                    except Exception:
                        continue
                    seen.add(identity)
                    entries.append((variant, item, record))
        if not entries:
            return []

        story: list[Any] = [
            PageBreak(),
            self._p("Evidence image appendix", "h1"),
            self._callout(
                "Point-in-time provider captures for visual review. Each image is "
                "linked to its source page and exact submitted variant; reopen the "
                "live record before clinical sign-off.",
                self.pale_blue,
            ),
            Spacer(1, 0.18 * cm),
        ]
        current_variant = ""
        for image_index, (variant, item, record) in enumerate(entries, start=1):
            variant_identity = (
                f"{variant.symbol} - {variant.hgvsc or variant.genomic_location}"
            )
            if variant_identity != current_variant:
                story.append(self._p(variant_identity, "h2"))
                current_variant = variant_identity
            image_path = Path(record["path"])
            width_px, height_px = ImageReader(str(image_path)).getSize()
            scale = min(
                (17.1 * cm) / width_px,
                (12.5 * cm) / height_px,
            )
            image = ReportImage(
                str(image_path),
                width=width_px * scale,
                height=height_px * scale,
            )
            image.hAlign = "LEFT"
            image_frame = Table([[image]], colWidths=[17.1 * cm])
            image_frame.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                        ("BOX", (0, 0), (-1, -1), 0.55, self.border),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            label = record.get("label") or "Provider evidence"
            caption = self._p(
                f"Figure {image_index}. {item.database} - {label}", "label"
            )
            metadata_parts = [f"Status: {item.status.replace('_', ' ').title()}"]
            captured_at = str(item.raw.get("captured_at") or "")
            if captured_at:
                metadata_parts.append(f"Captured: {captured_at}")
            source_url = record.get("url") or item.url
            source_text = escape(" | ".join(metadata_parts))
            if source_url:
                source_text += (
                    " | <link href="
                    + quoteattr(source_url)
                    + "><font color='#2F75B5'><u>Open live source</u></font></link>"
                )
            story.append(
                KeepTogether(
                    [
                        caption,
                        Spacer(1, 0.06 * cm),
                        image_frame,
                        Spacer(1, 0.06 * cm),
                        Paragraph(source_text, self.styles["small"]),
                    ]
                )
            )
            story.append(Spacer(1, 0.22 * cm))
        return story

    @staticmethod
    def _screenshot_records(item: DatabaseEvidence) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        raw_records = item.raw.get("screenshots")
        if isinstance(raw_records, list):
            for raw_record in raw_records:
                if not isinstance(raw_record, dict):
                    continue
                path = str(raw_record.get("path") or "").strip()
                if path:
                    records.append(
                        {
                            "label": str(raw_record.get("label") or ""),
                            "path": path,
                            "url": str(raw_record.get("url") or ""),
                        }
                    )
        legacy_path = str(item.raw.get("screenshot") or "").strip()
        if legacy_path and all(record["path"] != legacy_path for record in records):
            records.insert(
                0,
                {
                    "label": "Provider evidence",
                    "path": legacy_path,
                    "url": item.url,
                },
            )
        return records

    def _callout(self, text: str, fill: colors.Color) -> Table:
        table = Table([[self._p(text, "callout")]], colWidths=[17.1 * cm])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), fill),
                    ("BOX", (0, 0), (-1, -1), 0.5, self.border),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return table

    def _footer(self, patient_id: str, run_date: str):
        def draw(canvas, doc) -> None:
            canvas.saveState()
            canvas.setStrokeColor(self.border)
            canvas.setLineWidth(0.4)
            canvas.line(doc.leftMargin, 1.12 * cm, A4[0] - doc.rightMargin, 1.12 * cm)
            canvas.setFont("Helvetica", 7)
            canvas.setFillColor(self.muted)
            canvas.drawString(doc.leftMargin, 0.72 * cm, f"DIT {patient_id} - report {run_date}")
            canvas.drawRightString(
                A4[0] - doc.rightMargin,
                0.72 * cm,
                f"Page {doc.page}",
            )
            canvas.restoreState()

        return draw

    def _dit_validation_message(self, patient_id: str, run_date: str) -> str:
        if not DIT_PATTERN.fullmatch(patient_id):
            return (
                f"Identifier warning: {patient_id} does not match the expected "
                "DIT format YYOUM#####. Confirm patient identity before use."
            )
        report_year = re.match(r"(\d{4})", run_date or "")
        if report_year and patient_id[:2] != report_year.group(1)[-2:]:
            return (
                f"Identifier warning: DIT year prefix {patient_id[:2]} differs from "
                f"report year {report_year.group(1)[-2:]}. Confirm this is expected."
            )
        return ""

    def _p(self, value: Any, style: str) -> Paragraph:
        clean = "" if value is None else str(value)
        return Paragraph(escape(clean), self.styles[style])

    def _key(self, variant: VariantRecord) -> str:
        return f"{variant.sample}|{variant.hgvsc}"

    def _percentage(self, value: float | None) -> str:
        return "Not available" if value is None else f"{value:.2%}"

    def _depth_ao(self, variant: VariantRecord) -> str:
        depth = "-" if variant.depth is None else str(variant.depth)
        ao = "-" if variant.ao is None else str(variant.ao)
        return f"{depth} / {ao}"

    def _alleles(self, variant: VariantRecord) -> str:
        if not variant.ref_allele and not variant.alt_allele:
            return "Not available"
        return f"{variant.ref_allele or '-'} / {variant.alt_allele or '-'}"

    def _significance(self, item: DatabaseEvidence) -> str:
        significance = item.clinical_significance or "Not stated"
        if item.accession:
            return f"{significance}; accession {item.accession}"
        return significance

    def _evidence_summary(self, item: DatabaseEvidence) -> str:
        parts = [item.summary or "No summary returned"]
        captured_at = str(item.raw.get("captured_at") or "")
        if captured_at:
            parts.append(f"Captured {captured_at}")
        pipeline_version = str(item.raw.get("pipeline_version") or "")
        if pipeline_version:
            parts.append(f"Pipeline {pipeline_version}")
        cancer_type = str(item.raw.get("cancer_type") or "")
        if cancer_type:
            parts.append(f"Submitted cancer type {cancer_type}")
        return ". ".join(part.rstrip(".") for part in parts) + "."

    def _af_values(self, variant: VariantRecord) -> str:
        return f"{self._percentage(variant.af)} / {self._percentage(variant.gnomad_af)}"

    def _quality_caller(self, variant: VariantRecord) -> str:
        quality = "-" if variant.quality_score is None else f"{variant.quality_score:g}"
        return f"{quality} / {variant.source_caller or '-'}"

    def _known_ids(self, variant: VariantRecord) -> str:
        identifiers = [value for value in [variant.cosmic_id, variant.dbsnp_id] if value]
        return ", ".join(identifiers) or "Not available"
