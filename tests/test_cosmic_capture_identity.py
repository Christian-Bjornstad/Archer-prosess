from pathlib import Path

import pytest

from archer_processor.core.models import VariantRecord
from archer_processor.services.browser_review import BrowserReviewService


def requested_variant():
    return VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=1, sample="SYNTHETIC",
        symbol="TESTGENE", hgvsc="NM_999999.1:c.100G>A", hgvsp="p.Arg100Ser",
        cosmic_id="COSM123", genomic_location="chr17:1000", ref_allele="G", alt_allele="A",
    )


class CapturePage:
    url = "https://cancer.sanger.ac.uk/cosmic/mutation/overview?genome=37&id=999"

    def __init__(self, text):
        self.text = text
        self.captures = []

    def locator(self, selector):
        page = self

        class Locator:
            def inner_text(self, **kwargs):
                return page.text

            def count(self):
                return 1

            def fill(self, value):
                pass

            def all_inner_texts(self):
                return []

            def locator(self, selector):
                return self

            def screenshot(self, *, path):
                page.captures.append(path)

        return Locator()

    def wait_for_timeout(self, milliseconds):
        pass


@pytest.fixture
def service(tmp_path, monkeypatch):
    value = BrowserReviewService(profile_root=tmp_path)
    monkeypatch.setattr(value, "_cosmic_section", lambda page, heading: page.locator(heading))
    monkeypatch.setattr(value, "_write_audit", lambda *args: None)
    return value


@pytest.mark.parametrize("body", [
    "COSM1234\nOTHERGENE c.900A>T p.H300L",
    "PRECOSM123\nOTHERGENE c.900A>T p.H300L",
    "TESTGENE2 c.100G>A p.R100S",
    "TESTGENE-2 c.100G>A p.R100S",
    "TESTGENE c.100G>AA",
    "TESTGENE p.R100Sfs*3",
    "TESTGENE p.R100Ser",
])
def test_cosmic_rejects_identifier_gene_or_change_prefixes(service, tmp_path, body):
    page = CapturePage(body)

    evidence = service._capture_cosmic_result(requested_variant(), page, tmp_path)

    assert evidence.status == "identity_mismatch"
    assert not page.captures


@pytest.mark.parametrize("change", ["c.100G>A", "p.R100S"])
def test_cosmic_canonical_alias_accepts_exact_gene_and_change(service, tmp_path, change):
    page = CapturePage(f"Canonical COSMIC ID: COSM999\nTESTGENE {change}")

    evidence = service._capture_cosmic_result(requested_variant(), page, tmp_path)

    assert evidence.status == "found"
    assert len(page.captures) == 3


def test_cosmic_exact_identifier_still_supports_resolved_record(service, tmp_path):
    page = CapturePage("Mutation ID: COSM123\nOverview\nTissue distribution\nSamples")

    evidence = service._capture_cosmic_result(requested_variant(), page, tmp_path)

    assert evidence.status == "found"
    assert len(page.captures) == 3


@pytest.mark.parametrize("protein", ["p.?", "-", ".", "="])
def test_cosmic_unknown_protein_is_not_identity_proof(service, tmp_path, protein):
    item = requested_variant()
    item.hgvsp = protein
    page = CapturePage("TESTGENE\nProtein: ?\nAnnotation - pending. Classification = unknown")

    evidence = service._capture_cosmic_result(item, page, tmp_path)

    assert evidence.status == "identity_mismatch"
    assert not page.captures


@pytest.mark.parametrize("identity", [
    "c.100G>T p.R100S",
    "NM_888888.1:c.100G>A p.R100S",
    "NM_999999.1:c.100G>T NM_888888.1:c.100G>A p.R100S",
    "c.100G>A p.R100S chr17:g.1000G>T (GRCh37)",
    "c.100G>A p.R100S chr17:1000 G>T",
])
@pytest.mark.parametrize("prefix", [
    "Mutation ID: COSM123 ",
    "Canonical COSMIC ID: COSM999 ",
    "COSM123 has been merged into COSM999. ",
])
def test_cosmic_rejects_explicit_dna_conflicts_before_protein_fallback(
    service, tmp_path, identity, prefix, monkeypatch
):
    page = CapturePage(prefix + "TESTGENE " + identity)
    monkeypatch.setattr(service, "_verify_cosmic_grch37", lambda page: None)

    evidence = service._capture_cosmic_result(requested_variant(), page, tmp_path)

    assert evidence.status == "identity_mismatch"
    assert not page.captures


@pytest.mark.parametrize("identity", [
    "p.R100S",
    "NM_999999.2:c.100G>A p.R100S",
    "c.100G>A p.R100S chr17:g.1000G>A (GRCh37)",
    "c.100G>A p.R100S chr17:1000 G>A",
])
def test_cosmic_migration_keeps_nonconflicting_identity_fallbacks(
    service, tmp_path, identity, monkeypatch
):
    page = CapturePage("COSM123 has been merged into COSM999. TESTGENE " + identity)
    monkeypatch.setattr(service, "_verify_cosmic_grch37", lambda page: None)

    evidence = service._capture_cosmic_result(requested_variant(), page, tmp_path)

    assert evidence.status == "found"
    assert evidence.raw["cosmic_id_changed"] is True
    assert len(page.captures) == 3


def test_cosmic_does_not_invent_transcript_conflict_for_unqualified_input(service, tmp_path):
    requested = requested_variant()
    requested.hgvsc = "c.100G>A"
    page = CapturePage("Canonical COSMIC ID: COSM999 TESTGENE NM_999999.1:c.100G>A")

    evidence = service._capture_cosmic_result(requested, page, tmp_path)

    assert evidence.status == "found"
    assert len(page.captures) == 3
