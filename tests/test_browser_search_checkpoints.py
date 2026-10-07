import json
from dataclasses import replace
from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.services.browser_review import (
    BrowserReviewCancelled,
    BrowserReviewService,
)


def variants():
    first = VariantRecord(
        source_file=Path("synthetic.tsv"),
        source_row=1,
        sample="SYNTHETIC_VPM_1",
        symbol="TP53",
        hgvsc="NM_000546.6:c.524G>A",
        hgvsp="p.Arg175His",
        genomic_location="chr17:7578406",
        ref_allele="G",
        alt_allele="A",
        cosmic_id="COSM10648",
    )
    return [first, replace(first, source_row=2, sample="SYNTHETIC_VPM_2")]


def isolated_service(tmp_path, monkeypatch, lookup, *, stop_requested=None):
    service = BrowserReviewService(
        profile_root=tmp_path / "profiles",
        request_delay_ms=0,
        request_delay_max_ms=0,
        provider_switch_delay_ms=0,
        stop_requested=stop_requested,
    )

    class Page:
        url = ""

        def goto(self, url, **kwargs):
            self.url = url

    class Context:
        pages = [Page()]
        closed = False

        def close(self):
            self.closed = True

    context = Context()

    class Runtime:
        def __init__(self):
            self.chromium = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def launch_persistent_context(self, *args, **kwargs):
            return context

    monkeypatch.setattr(
        service, "_browser_api", lambda: (lambda: Runtime(), Exception, TimeoutError)
    )
    monkeypatch.setattr(service, "_lookup_oncokb_variant", lookup)
    monkeypatch.setattr(service, "_lookup_cosmic_variant", lookup)
    monkeypatch.setattr(service, "_lookup_clinvar_variant", lookup)
    monkeypatch.setattr(service, "_resolve_franklin_queries", lookup)
    monkeypatch.setattr(service, "_ensure_cosmic_grch37", lambda page: None)
    monkeypatch.setattr(service, "_try_saved_login", lambda database, page: True)
    monkeypatch.setattr(service, "_wait_between_queries", lambda *args, **kwargs: None)
    return service, context


@pytest.mark.parametrize(
    "database,status",
    [
        ("OncoKB", "found"),
        ("COSMIC", "error"),
        ("ClinVar", "partial_capture"),
        ("Franklin", "not_found"),
    ],
)
def test_each_provider_persists_and_checkpoints_before_next_variant(
    tmp_path, monkeypatch, database, status
):
    requested = variants()
    checkpoints = []
    calls = []
    artifact_root = tmp_path / "evidence"

    def lookup(page, variant, directory, **kwargs):
        if calls:
            assert len(checkpoints) == 1
            assert next(iter(checkpoints[0].values()))[0].status == status
            audits = list((artifact_root / database.lower()).glob("*.audit.json"))
            assert len(audits) == 1
            assert json.loads(audits[0].read_text(encoding="utf-8"))["status"] == status
        calls.append(variant.sample)
        return DatabaseEvidence(
            database, status, "Finished variant.", raw={"nested": {"values": [1]}}
        )

    service, context = isolated_service(tmp_path, monkeypatch, lookup)

    results = service.search_variants(
        requested, [database], artifact_root, checkpoint=checkpoints.append
    )

    assert len(checkpoints) == 2
    assert list(checkpoints[0]) == [service.variant_key(requested[0])]
    assert list(checkpoints[1]) == [service.variant_key(requested[1])]
    assert all(len(evidence) == 1 for evidence in results.values())
    next(iter(checkpoints[0].values()))[0].raw["nested"]["values"].append(2)
    assert results[service.variant_key(requested[0])][0].raw["nested"]["values"] == [1]
    assert context.closed


def test_cancellation_retains_completed_variant_and_clears_checkpoint_callback(
    tmp_path, monkeypatch
):
    requested = variants()
    stopped = False
    checkpoints = []
    calls = []

    def lookup(page, variant, directory, **kwargs):
        calls.append(variant.sample)
        return DatabaseEvidence("OncoKB", "found", "Finished variant.")

    def checkpoint(result):
        nonlocal stopped
        checkpoints.append(result)
        stopped = True

    service, context = isolated_service(
        tmp_path, monkeypatch, lookup, stop_requested=lambda: stopped
    )

    with pytest.raises(BrowserReviewCancelled):
        service.search_variants(
            requested, ["OncoKB"], tmp_path / "evidence", checkpoint=checkpoint
        )

    assert calls == ["SYNTHETIC_VPM_1"]
    assert len(checkpoints) == 1
    assert checkpoints[0][service.variant_key(requested[0])][0].status == "found"
    assert len(list((tmp_path / "evidence" / "oncokb").glob("*.audit.json"))) == 1
    assert context.closed

    stopped = False
    service.search_variants(requested[:1], ["OncoKB"], tmp_path / "next-run")
    assert len(checkpoints) == 1
