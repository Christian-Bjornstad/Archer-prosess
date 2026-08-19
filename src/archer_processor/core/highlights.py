from __future__ import annotations

from typing import Any

from archer_processor.core.models import VariantRecord


def variant_highlight(variant: VariantRecord) -> str:
    if _is_artifact(variant):
        return "artifact"
    if _variant_sum(variant, "Tier I", "Tier II") > 5:
        return "tier"
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


def _variant_sum(variant: VariantRecord, *columns: str) -> float:
    """Sum values from variant.raw (TSV columns) first, fall back to history_matches."""
    total = 0.0
    # First check raw TSV columns
    for column in columns:
        if column in variant.raw:
            total += _number(variant.raw.get(column))
    # If no raw data, fall back to history_matches
    if total == 0.0:
        for match in variant.history_matches:
            for column in columns:
                total += _number(match.get(column))
    return total


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