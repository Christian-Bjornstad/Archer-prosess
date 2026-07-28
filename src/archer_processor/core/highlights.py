from __future__ import annotations

from typing import Any

from archer_processor.core.models import VariantRecord


def variant_highlight(variant: VariantRecord) -> str:
    if _is_artifact(variant):
        return "artifact"
    if _history_sum(variant, "Tier I", "Tier II") >= 5:
        return "tier"
    if _history_sum(variant, "Germ") >= 10:
        return "germline"
    return ""


def _is_artifact(variant: VariantRecord) -> bool:
    if _truthy(variant.artifact_status):
        return True
    if any("artifact" in rule.lower() for rule in variant.matched_rules):
        return True
    return _history_sum(variant, "Artf") > 0


def _history_sum(variant: VariantRecord, *columns: str) -> float:
    total = 0.0
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
