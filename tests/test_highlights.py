from pathlib import Path

from archer_processor.core.highlights import priority_warning, variant_highlight
from archer_processor.core.models import VariantRecord


def variant(**kwargs) -> VariantRecord:
    values = {
        "source_file": Path("input.tsv"),
        "source_row": 2,
        "sample": "26OUM00001",
        "symbol": "FLT3",
        "hgvsc": "NM_004119.2:c.1419-4dup",
    }
    values.update(kwargs)
    return VariantRecord(**values)


def test_artifact_highlight_is_orange_category():
    assert variant_highlight(variant(matched_rules=["flt3_1419_4dup_artifact"])) == "artifact"
    # Only user-configured artifact rules (matched_rules) count as artifacts
    assert variant_highlight(variant(artifact_status="Yes")) == ""
    assert variant_highlight(variant(history_matches=[{"Artf": 1}])) == ""


def test_tier_highlight_requires_sum_above_five():
    assert variant_highlight(variant(history_matches=[{"Tier I": 2, "Tier II": 3}])) == ""
    assert variant_highlight(variant(history_matches=[{"Tier I": 3, "Tier II": 3}])) == "tier"


def test_germline_highlight_requires_count_above_ten_and_uses_af_strength():
    assert variant_highlight(variant(history_matches=[{"Germ": 10}], af=0.8)) == ""
    assert variant_highlight(variant(history_matches=[{"Germ": 11}], af=0.3499)) == "germline_low_af"
    assert variant_highlight(variant(history_matches=[{"Germ": 11}], af=0.35)) == "germline"
    assert variant_highlight(variant(history_matches=[{"Germ": 11}], af=None)) == ""


def test_artifact_highlight_wins_over_priority_highlights():
    record = variant(
        matched_rules=["known_artifact"],
        history_matches=[{"Tier I": 6, "Germ": 11}],
        af=0.5,
    )
    assert variant_highlight(record) == "artifact"


def test_missing_af_priority_warning_is_exposed_without_mutating_variant():
    record = variant(history_matches=[{"Germ": 11}], af=None)

    assert priority_warning(record) == (
        "Germline priority could not be colored because AF is missing."
    )
    assert record.warnings == []


def test_no_other_rows_are_colored():
    assert variant_highlight(variant(decision="included")) == ""
    assert variant_highlight(variant(warnings=["Review this"])) == ""
    assert variant_highlight(variant(history_matches=[{"Tier I": 2, "Tier II": 2, "Germ": 9, "Artf": 0}])) == ""
