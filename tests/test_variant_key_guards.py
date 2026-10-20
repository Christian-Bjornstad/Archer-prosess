import csv
from dataclasses import replace
from pathlib import Path

import openpyxl
import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.core.processor import VariantProcessor
from archer_processor.services.browser_review import BrowserReviewService
from archer_processor.services.database_search import DatabaseSearchService
from archer_processor.services.processed_workbook import ProcessedWorkbookLoader
from archer_processor.services.settings import AppSettings


HEADERS = ["Sample", "Symbol", "HGVSc", "HGVSp", "AF", "Depth", "AO", "Type", "Source", "Genomic Location", "Ref/Alt Allele"]


def candidates():
    first = VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=2, sample="DEMO_VPM_1",
        symbol="TP53", hgvsc="", hgvsp="NP_000537.3:p.Arg175His",
        genomic_location="chr17:7578406", ref_allele="G", alt_allele="A",
    )
    second = replace(first, source_row=3, hgvsp="p.Arg248Gln", genomic_location="chr17:7577538")
    return [first, second]


def cells(candidate):
    return [candidate.sample, candidate.symbol, candidate.hgvsc, candidate.hgvsp, 0.3, 100, 30, "SNV", "Synthetic", candidate.genomic_location, f"{candidate.ref_allele}/{candidate.alt_allele}"]


def tsv_file(tmp_path, requested):
    path = tmp_path / "synthetic.tsv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(HEADERS)
        writer.writerows(cells(candidate) for candidate in requested)
    return path


def workbook_file(tmp_path, requested):
    path = tmp_path / "synthetic.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "With Artifacts"
    sheet.append([*HEADERS, "Skip Database Search (X)"])
    for candidate in requested:
        sheet.append([*cells(candidate), ""])
    workbook.save(path)
    workbook.close()
    return path


def test_import_rejects_distinct_variants_with_the_same_missing_hgvsc_key(tmp_path):
    path = tsv_file(tmp_path, candidates())

    with pytest.raises(ValueError, match="Distinct variants.*DEMO_VPM_1.*rows 2 and 3"):
        VariantProcessor().process(path, "2026-10-07")


def test_resume_rejects_distinct_variants_before_restoring_merged_evidence(tmp_path):
    path = workbook_file(tmp_path, candidates())

    with pytest.raises(ValueError, match="Distinct variants.*DEMO_VPM_1"):
        ProcessedWorkbookLoader().load(path)


def test_browser_search_rejects_key_collision_before_opening_a_browser(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path / "profiles")
    monkeypatch.setattr(service, "_browser_api", lambda: pytest.fail("Rejected variants must not open a browser."))

    with pytest.raises(ValueError, match="Distinct variants.*DEMO_VPM_1"):
        service.search_variants(candidates(), ["ClinVar"], tmp_path / "evidence")

    assert not (tmp_path / "profiles").exists()
    assert not (tmp_path / "evidence").exists()


def test_parallel_api_search_rejects_key_collision_before_any_lookup(monkeypatch):
    service = DatabaseSearchService(AppSettings())
    lookups = []
    monkeypatch.setattr(service, "search_variant", lambda variant, databases: lookups.append(variant) or [DatabaseEvidence("ClinVar", "not_found")])

    with pytest.raises(ValueError, match="Distinct variants.*DEMO_VPM_1"):
        service.search_variants_parallel(candidates(), ["ClinVar"])

    assert lookups == []


@pytest.mark.parametrize("changes", [
    {},
    {"symbol": "tp53", "hgvsp": "p.R175H", "genomic_location": "17:7578406-7578406", "ref_allele": "g", "alt_allele": "a"},
    {"hgvsp": "", "genomic_location": "", "ref_allele": "", "alt_allele": ""},
])
def test_import_preserves_duplicates_without_identity_contradictions(tmp_path, changes):
    first = candidates()[0]
    second = replace(first, source_row=3, **changes)
    path = tsv_file(tmp_path, [first, second])

    result = VariantProcessor().process(path, "2026-10-07")

    assert result.total_count == 2
    assert [candidate.source_row for candidate in result.variants] == [2, 3]


@pytest.mark.parametrize("changes", [
    {"symbol": "BRAF"}, {"hgvsp": "p.Arg248Gln"},
    {"genomic_location": "chr17:7578407"}, {"ref_allele": "T"}, {"alt_allele": "C"},
])
def test_import_rejects_each_explicit_identity_contradiction(tmp_path, changes):
    first = replace(candidates()[0], hgvsc="NM_000546.6:c.524G>A")
    second = replace(first, source_row=3, **changes)
    path = tsv_file(tmp_path, [first, second])

    with pytest.raises(ValueError, match="Distinct variants"):
        VariantProcessor().process(path, "2026-10-07")


def test_missing_optional_fields_do_not_hide_a_later_contradiction(tmp_path):
    first = candidates()[0]
    incomplete = replace(first, source_row=3, hgvsp="", genomic_location="", ref_allele="", alt_allele="")
    conflicting = replace(first, source_row=4, alt_allele="T")
    path = tsv_file(tmp_path, [first, incomplete, conflicting])

    with pytest.raises(ValueError, match="Distinct variants.*rows 2 and 4"):
        VariantProcessor().process(path, "2026-10-07")
