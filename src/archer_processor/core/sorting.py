from __future__ import annotations

from archer_processor.core.models import VariantRecord


def variant_sort_key(variant: VariantRecord) -> tuple[str, int, float, str, str]:
    """Group by patient and sort known AF values from highest to lowest."""
    missing_af = 1 if variant.af is None else 0
    descending_af = 0.0 if variant.af is None else -variant.af
    return (
        variant.patient_id,
        missing_af,
        descending_af,
        variant.symbol,
        variant.hgvsc,
    )
