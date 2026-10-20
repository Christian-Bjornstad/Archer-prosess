from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.core.evidence_display import evidence_display_text, historical_attempt_detail
from archer_processor.gui.app import MainWindow
from archer_processor.services.browser_review import BrowserReviewService
from archer_processor.services.evidence_history import current_attempt


def variant():
    return VariantRecord(
        Path("synthetic.tsv"), 2, "SYNTHETIC", "TESTGENE", "NM_999999.1:c.10G>A",
        genomic_location="chr1:10010", ref_allele="G", alt_allele="A",
    )


def active_window(requested, evidence):
    key = BrowserReviewService.variant_key(requested)
    return SimpleNamespace(
        result=SimpleNamespace(variants=[requested]),
        evidence={key: [evidence]},
        _active_sources={(requested.patient_id, evidence.database)},
        _search_pending_pairs={(key, evidence.database)},
    )


def historical_pending():
    pending = DatabaseEvidence(
        "MTBP", "timeout", "Original report remains queued",
        raw={"analysis_id": "ARCHER-current", "submitted_query": "TESTGENE:c.10G>A"},
    )
    return DatabaseEvidence(
        "MTBP", "historical", "Previously verified result",
        raw={
            **deepcopy(pending.raw),
            "historical_result": {
                "captured_at": "2026-09-01T12:00:00+00:00",
                "current_attempt": asdict(pending),
            },
        },
    )


@pytest.mark.parametrize("message", ["Edge unavailable", "Browser timed out"])
def test_global_failure_preserves_historical_mtbp_and_resumes_its_current_report(
    tmp_path, monkeypatch, message,
):
    requested = variant()
    key = BrowserReviewService.variant_key(requested)
    previous = historical_pending()
    before = asdict(previous)
    window = active_window(requested, previous)

    MainWindow._record_active_search_failure(window, message)

    retained = window.evidence[key][0]
    assert retained.status == "historical"
    assert retained.raw["historical_result"] == before["raw"]["historical_result"]
    assert retained.raw["analysis_id"] == "ARCHER-current"
    assert retained.raw["interrupted_search"]["message"] == message
    assert asdict(previous) == before
    service = BrowserReviewService(profile_root=tmp_path)
    recoveries = []

    def recover(items, *args, **kwargs):
        recoveries.extend(items)
        return {key: DatabaseEvidence(
            "MTBP", "found", "Recovered current report",
            raw={"analysis_id": "ARCHER-current", "remote_report_cleanup": {"status": "deleted"}},
        )}

    monkeypatch.setattr(service, "_recover_mtbp_timeouts", recover)
    monkeypatch.setattr(
        service, "_search_mtbp_batch",
        lambda *args, **kwargs: pytest.fail("A retained report must not be submitted twice"),
    )

    result = service._search_mtbp(
        [requested], tmp_path, progress=None, prior_evidence=window.evidence,
    )

    assert len(recoveries) == 1
    assert recoveries[0][1].status == "timeout"
    assert result[key].summary == "Recovered current report"


@pytest.mark.parametrize("status", ["timeout", "partial_capture", "submission_unknown", "found"])
def test_global_failure_keeps_original_pending_mtbp_recovery_status(status):
    requested = variant()
    previous = DatabaseEvidence(
        "MTBP", status, "Current report needs recovery",
        raw={"analysis_id": "ARCHER-current"},
    )
    window = active_window(requested, previous)

    MainWindow._record_active_search_failure(window, "Edge unavailable")

    retained = window.evidence[BrowserReviewService.variant_key(requested)][0]
    assert retained.status == status
    assert retained.summary == "Current report needs recovery"
    assert retained.raw["interrupted_search"]["message"] == "Edge unavailable"


@pytest.mark.parametrize("database", ["ClinVar", "Franklin", "MTBP"])
@pytest.mark.parametrize("message", ["Edge unavailable", "Browser timed out"])
def test_global_failure_keeps_old_classification_visibly_historical(database, message):
    requested = variant()
    previous = DatabaseEvidence(
        database, "historical", "Old verified result", clinical_significance="Pathogenic",
        raw={
            "captured_at": "2026-09-01T12:00:00+00:00",
            "screenshots": [{"path": "old_capture.png"}],
            "historical_result": {
                "captured_at": "2026-09-01T12:00:00+00:00",
                "used_at": "2026-10-01T12:00:00+00:00",
                "current_attempt": {"database": database, "status": "not_found", "summary": "Old attempt"},
            },
        },
    )
    before = asdict(previous)
    window = active_window(requested, previous)

    MainWindow._record_active_search_failure(window, message)

    item = window.evidence[BrowserReviewService.variant_key(requested)][0]
    assert item.status == "historical"
    assert item.summary == "Old verified result"
    assert item.raw["historical_result"]["captured_at"] == "2026-09-01T12:00:00+00:00"
    assert item.raw["screenshots"] == [{"path": "old_capture.png"}]
    displayed = evidence_display_text(item, item.clinical_significance)
    assert "Tidligere resultat" in displayed and "01.09.2026" in displayed
    assert "Pathogenic" in displayed
    attempt = current_attempt(item)
    assert attempt.status == ("timeout" if "timed out" in message else "error")
    assert attempt.summary == f"Browser lookup interrupted: {message}"
    assert attempt.clinical_significance == ""
    assert "screenshots" not in attempt.raw and "historical_result" not in attempt.raw
    assert message in historical_attempt_detail(item)
    assert asdict(previous) == before


def test_global_failure_records_error_normally_for_nonhistorical_result():
    requested = variant()
    window = active_window(requested, DatabaseEvidence("ClinVar", "partial_capture", "Partial result"))

    MainWindow._record_active_search_failure(window, "Edge unavailable")

    item = window.evidence[BrowserReviewService.variant_key(requested)][0]
    assert item.status == "error"
    assert item.summary == "Browser lookup interrupted: Edge unavailable"
