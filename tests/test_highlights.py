from pathlib import Path

from archer_processor.core.highlights import variant_highlight
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
    assert variant_highlight(variant(artifact_status="Yes")) == "artifact"
    assert variant_highlight(variant(history_matches=[{"Artf": 1}])) == "artifact"


def test_tier_one_and_two_sum_of_five_is_yellow_category():
    assert variant_highlight(variant(history_matches=[{"Tier I": 2, "Tier II": 3}])) == "tier"


def test_germline_ten_or_above_is_green_category():
    assert variant_highlight(variant(history_matches=[{"Germ": 10}])) == "germline"


def test_no_other_rows_are_colored():
    assert variant_highlight(variant(decision="included")) == ""
    assert variant_highlight(variant(warnings=["Review this"])) == ""
    assert variant_highlight(variant(history_matches=[{"Tier I": 2, "Tier II": 2, "Germ": 9, "Artf": 0}])) == ""
