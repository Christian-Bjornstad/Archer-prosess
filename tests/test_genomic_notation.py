from archer_processor.services.genomic_notation import format_mtbp_grch37


def test_format_mtbp_grch37_handles_substitution_and_both_indel_anchor_sides():
    assert format_mtbp_grch37("chr19:33792996", "G", "A") == (
        "chr19:g.33792996G>A"
    )
    assert format_mtbp_grch37("chr13:28609813", "GA", "G") == (
        "chr13:g.28609814del"
    )
    assert format_mtbp_grch37("chr13:28609813", "G", "GA") == (
        "chr13:g.28609813_28609814insA"
    )
    assert format_mtbp_grch37("chr13:28609813", "GA", "A") == (
        "chr13:g.28609813del"
    )


def test_format_mtbp_grch37_rejects_ambiguous_or_unusable_alleles():
    assert format_mtbp_grch37("", "G", "A") == ""
    assert format_mtbp_grch37("chr13:28609813", "G", "G") == ""
    assert format_mtbp_grch37("chr13:28609813", "N", "A") == ""
    assert format_mtbp_grch37("chr13:28609813", "G", "<DEL>") == ""
