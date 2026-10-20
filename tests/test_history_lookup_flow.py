from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.gui.app import (
    BrowserReviewWorker, _completed_evidence_sources, _protected_remote_evidence_sources,
    _retryable_source_pairs,
)
from archer_processor.services.browser_review import BrowserReviewService, parse_oncokb_page
from archer_processor.services.settings import AppSettings
from archer_processor.services.evidence_history import EvidenceHistoryError, EvidenceHistoryStore


def variant(row=1):
    return VariantRecord(Path("synthetic.tsv"), row, f"SYNTHETIC-{row}", "TESTGENE",
                         "NM_999999.1:c.100G>A", genomic_location="chr17:1000",
                         ref_allele="G", alt_allele="A", hgvsp="p.Arg34Ser")


class History:
    def __init__(self):
        self.recorded = []

    def record(self, item, evidence, **kwargs):
        self.recorded.append((item, deepcopy(evidence)))
        return evidence.status == "found"

    def fallback(self, item, current, artifact_directory, **kwargs):
        if current.status == "found":
            return current
        result = deepcopy(current)
        result.status = "historical"
        result.clinical_significance = "Previously verified classification"
        result.raw["historical_result"] = {
            "captured_at": "2025-01-02T12:30:00+00:00",
            "current_attempt": asdict(current),
        }
        return result


class Service:
    def __init__(self, outcome, *, retry_succeeds=False):
        self.calls = 0
        self.outcome = outcome
        self.retry_succeeds = retry_succeeds

    def search_variants(self, variants, databases, artifact_root, **kwargs):
        self.calls += 1
        return {
            BrowserReviewService.variant_key(item): [
                DatabaseEvidence(database, "found", "Fresh result")
                if self.retry_succeeds and self.calls == 2 else deepcopy(self.outcome)
                for database in databases
                if (BrowserReviewService.variant_key(item), database)
                not in kwargs.get("completed_sources", set())
            ] for item in variants
        }


def test_browser_records_a_published_success_once_and_preserves_result(tmp_path, monkeypatch):
    history = History()
    service = BrowserReviewService(profile_root=tmp_path, history_store=history)
    requested = variant()
    result = DatabaseEvidence("ClinVar", "found", raw={"captured_at": "2025-01-02T12:30:00+00:00"})

    def search(database, variants, artifact_directory, **kwargs):
        service._publish_variant_result(requested, result)
        return {service.variant_key(requested): result}

    monkeypatch.setattr(service, "_search_database", search)
    actual = service.search_variants([requested], ["ClinVar"], tmp_path / "evidence")

    assert len(history.recorded) == 1
    assert actual[service.variant_key(requested)][0] is result
    assert result.raw["captured_at"] == "2025-01-02T12:30:00+00:00"


@pytest.mark.parametrize("retry_succeeds", [False, True])
def test_history_is_applied_only_after_live_retry_and_preserves_checkpoint(
    qt_app, tmp_path, retry_succeeds,
):
    requested = variant()
    key = BrowserReviewService.variant_key(requested)
    history = History()
    worker = BrowserReviewWorker([requested], ["OncoKB"], tmp_path, AppSettings(), history_store=history)
    service = Service(DatabaseEvidence("OncoKB", "error", "Current site failed"), retry_succeeds=retry_succeeds)
    updates = []
    worker.evidence_updated.connect(updates.append)

    result = worker._search_patient_lanes(
        requested.patient_id, [requested], 1, "Synthetic", services={"fast databases": service},
    )

    assert service.calls == 2
    item = result[key][0]
    if retry_succeeds:
        assert item.status == "found" and item.summary == "Fresh result"
    else:
        assert item.status == "historical"
        assert item.raw["historical_result"]["current_attempt"]["summary"] == "Current site failed"
        assert updates[-1][key][0].status == "historical"
        assert (key, "OncoKB") not in _completed_evidence_sources(result)
        assert _retryable_source_pairs([requested], result, ["OncoKB"]) == {(key, "OncoKB")}


def test_history_does_not_disable_provider_pause_or_deferred_results(qt_app, tmp_path):
    requested = [variant(index) for index in (1, 2, 3)]
    worker = BrowserReviewWorker(requested, ["OncoKB"], tmp_path, AppSettings(), history_store=History())
    service = Service(DatabaseEvidence("OncoKB", "error", "Site unavailable"))

    for index, item in enumerate(requested, 1):
        result = worker._search_patient_lanes(item.patient_id, [item], index, "Synthetic", services={"fast databases": service})
        evidence = result[BrowserReviewService.variant_key(item)][0]
        assert evidence.status == "historical"
        assert evidence.raw["historical_result"]["current_attempt"]["status"] == ("deferred" if index == 3 else "error")

    assert service.calls == 4
    assert "OncoKB" in worker._paused_database_reasons


def test_historical_mtbp_keeps_pending_report_protection_and_resumes(qt_app, tmp_path, monkeypatch):
    requested = variant()
    key = BrowserReviewService.variant_key(requested)
    pending = DatabaseEvidence("MTBP", "timeout", "Still queued", raw={"analysis_id": "ARCHER-current", "submitted_query": requested.hgvsc})
    worker = BrowserReviewWorker([requested], ["MTBP"], tmp_path, AppSettings(), history_store=History())
    result = worker._search_patient_lanes(requested.patient_id, [requested], 1, "Synthetic", services={"MTBP": Service(pending)})
    assert result[key][0].status == "historical"
    assert "MTBP" in worker._paused_database_reasons
    assert _protected_remote_evidence_sources(result) == {(key, "MTBP")}

    service = BrowserReviewService(profile_root=tmp_path)
    recovered = []
    def recover(items, *args, **kwargs):
        recovered.extend(items)
        return {key: pending}
    monkeypatch.setattr(service, "_recover_mtbp_timeouts", recover)
    monkeypatch.setattr(service, "_search_mtbp_batch", lambda *args, **kwargs: pytest.fail("must not resubmit a pending report"))
    service._search_mtbp([requested], tmp_path, progress=None, prior_evidence=result)
    assert recovered and recovered[0][1].status == "timeout"


def test_successful_oncokb_browser_match_has_reusable_identity_proof():
    requested = variant()
    evidence = parse_oncokb_page(
        "TESTGENE R34S\nOncogenicity\nOncogenic\nVariant Overview\nEvidence\nMutation Effect",
        requested, "https://www.oncokb.org/gene/TESTGENE/somatic/R34S",
    )
    assert evidence.status == "found"
    assert evidence.raw["identity_verification"]["accepted"] is True


def test_provider_startup_failure_can_use_backup_without_losing_current_error(qt_app, tmp_path, monkeypatch):
    from archer_processor.services.browser_review import BrowserAutomationUnavailable
    requested = variant()
    key = BrowserReviewService.variant_key(requested)
    history = History()
    service = BrowserReviewService(profile_root=tmp_path, history_store=history)
    def unavailable(*args, **kwargs):
        raise BrowserAutomationUnavailable("Edge could not start")
    monkeypatch.setattr(service, "_search_database", unavailable)
    worker = BrowserReviewWorker([requested], ["ClinVar"], tmp_path, AppSettings(), history_store=history)

    result = worker._search_patient_lanes(requested.patient_id, [requested], 1, "Synthetic", services={"ClinVar": service})

    evidence = result[key][0]
    assert evidence.status == "historical"
    attempt = evidence.raw["historical_result"]["current_attempt"]
    assert attempt["status"] == "error"
    assert "Edge could not start" in attempt["summary"]


def test_history_storage_failure_never_discards_live_result(qt_app, tmp_path, monkeypatch):
    class BrokenHistory(History):
        def record(self, *args, **kwargs):
            raise EvidenceHistoryError("Disk unavailable")
        def fallback(self, *args, **kwargs):
            raise EvidenceHistoryError("Disk unavailable")
    requested = variant()
    key = BrowserReviewService.variant_key(requested)
    history = BrokenHistory()
    service = BrowserReviewService(profile_root=tmp_path, history_store=history)
    monkeypatch.setattr(service, "_search_database", lambda *args, **kwargs: {key: DatabaseEvidence("ClinVar", "found", "Live")})
    messages = []
    assert service.search_variants([requested], ["ClinVar"], tmp_path, progress=messages.append)[key][0].status == "found"
    worker = BrowserReviewWorker([requested], ["ClinVar"], tmp_path, AppSettings(), history_store=history)
    worker.status.connect(messages.append)
    result = worker._search_patient_lanes(requested.patient_id, [requested], 1, "Synthetic", services={"ClinVar": Service(DatabaseEvidence("ClinVar", "not_found"))})
    assert result[key][0].status == "not_found"
    assert any("backup unavailable" in message for message in messages)


def test_real_history_flows_from_live_capture_to_failed_lookup_and_workbook(qt_app, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from PIL import Image, ImageDraw
    from archer_processor.services.evidence_audit import EvidenceAuditIndex
    requested = variant()
    key = BrowserReviewService.variant_key(requested)
    screenshot = tmp_path / "clinical.png"
    image = Image.new("RGB", (400, 180), "white")
    ImageDraw.Draw(image).text((20, 20), "Synthetic clinical classification", fill="black")
    image.save(screenshot)
    original_time = "2025-01-02T12:30:00+00:00"
    original = DatabaseEvidence("ClinVar", "found", "Verified old result", raw={
        "captured_at": original_time, "assembly_verified": "GRCh37",
        "identity_verification": {"accepted": True},
        "screenshots": [{"label": "Classification summary", "path": str(screenshot)}],
    })
    history = EvidenceHistoryStore(tmp_path / "history", clock=lambda: datetime(2026, 10, 15, tzinfo=timezone.utc))
    service = BrowserReviewService(profile_root=tmp_path, history_store=history)
    monkeypatch.setattr(service, "_search_database", lambda *args, **kwargs: {key: original})
    service.search_variants([requested], ["ClinVar"], tmp_path / "old")
    screenshot.unlink()
    worker = BrowserReviewWorker([requested], ["ClinVar"], tmp_path / "new", AppSettings(), history_store=history)
    result = worker._search_patient_lanes(requested.patient_id, [requested], 1, "Synthetic", services={"ClinVar": Service(DatabaseEvidence("ClinVar", "not_found", "Live empty result"))})
    assert result[key][0].status == "historical"
    assert result[key][0].raw["historical_result"]["captured_at"] == original_time
    assert Path(result[key][0].raw["screenshots"][0]["path"]).exists()
    restored_audit = EvidenceAuditIndex.build(tmp_path / "new").best("ClinVar", requested)
    assert restored_audit[1]["status"] == "historical"
    assert restored_audit[1]["raw"]["historical_result"]["current_attempt"]["status"] == "not_found"


def test_provider_exception_keeps_published_capture_and_continues_next_source(tmp_path, monkeypatch):
    first, second = variant(1), variant(2)
    first_key, second_key = map(BrowserReviewService.variant_key, (first, second))
    service = BrowserReviewService(profile_root=tmp_path, provider_switch_delay_ms=0)
    found = DatabaseEvidence("OncoKB", "found", "Captured before failure")
    calls = []
    def search(database, variants, artifact_directory, **kwargs):
        calls.append(database)
        if database == "OncoKB":
            service._publish_variant_result(first, found)
            raise RuntimeError("Connection lost")
        return {service.variant_key(item): DatabaseEvidence(database, "not_found") for item in variants}
    monkeypatch.setattr(service, "_search_database", search)

    result = service.search_variants([first, second], ["OncoKB", "ClinVar"], tmp_path)

    assert calls == ["OncoKB", "ClinVar"]
    assert result[first_key][0] is found
    assert result[second_key][0].status == "error"
    assert result[second_key][1].status == "not_found"


def test_provider_cancellation_is_not_converted_into_history_error(tmp_path, monkeypatch):
    from archer_processor.services.browser_review import BrowserReviewCancelled
    service = BrowserReviewService(profile_root=tmp_path)
    def cancel(*args, **kwargs):
        raise BrowserReviewCancelled()
    monkeypatch.setattr(service, "_search_database", cancel)
    with pytest.raises(BrowserReviewCancelled):
        service.search_variants([variant()], ["ClinVar"], tmp_path)


def test_provider_exception_preserves_retained_mtbp_ownership(tmp_path, monkeypatch):
    requested = variant()
    key = BrowserReviewService.variant_key(requested)
    prior = DatabaseEvidence("MTBP", "timeout", "Report pending", raw={"analysis_id": "ARCHER-current"})
    service = BrowserReviewService(profile_root=tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("Connection lost")
    monkeypatch.setattr(service, "_search_database", fail)

    result = service.search_variants([requested], ["MTBP"], tmp_path, prior_evidence={key: [prior]})

    assert result[key][0].status == "timeout"
    assert result[key][0].raw["analysis_id"] == "ARCHER-current"
    assert "recovery_error" in result[key][0].raw
    assert "recovery_error" not in prior.raw
