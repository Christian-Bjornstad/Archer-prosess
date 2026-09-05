from __future__ import annotations

from typing import Any

from archer_processor.core.models import VariantRecord


def variant_highlight(variant: VariantRecord) -> str:
    if _is_artifact(variant):
        if (
            variant.symbol.upper() == "ASXL1"
            and variant.hgvsc == "NM_015338.5:c.1934dup"
            and variant.af is not None
            and 0.05 < variant.af <= 0.055
        ):
            return "artifact_light"
        return "artifact"
    if _variant_sum(variant, "Germ") > 10 and variant.af is not None:
        return "germline" if variant.af >= 0.35 else "germline_low_af"
    return ""


def priority_warning(variant: VariantRecord) -> str:
    if _variant_sum(variant, "Germ") > 10 and variant.af is None:
        return "Germline priority could not be colored because AF is missing."
    return ""


def _is_artifact(variant: VariantRecord) -> bool:
    # Only consider artifacts from user-configured rules (matched_rules)
    return any("artifact" in rule.lower() for rule in variant.matched_rules)


def is_report_artifact(variant: VariantRecord) -> bool:
    """True for variants the patient report should omit from all interpretation
    sheets (Oversikt, Vedlegg, per-variant pages).  Artifacts stay in the
    hidden Data sheet for traceability."""
    return _is_artifact(variant) and variant.decision == "excluded"


def _variant_sum(variant: VariantRecord, *columns: str) -> float:
    """Sum priority counts supplied directly by the Archer TSV."""
    return sum(_number(variant.raw.get(column)) for column in columns)


def _number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return 0.0


def _truthy(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text not in {"", "0", "false", "no", "nei", "none", "nan"}
