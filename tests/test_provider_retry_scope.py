from pathlib import Path

import pytest

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.gui.app import BrowserReviewWorker
from archer_processor.services.browser_review import (
    BrowserReviewCancelled,
    BrowserReviewService,
)
from archer_processor.services.settings import AppSettings


def variant(row: int) -> VariantRecord:
    return VariantRecord(
        source_file=Path("synthetic.tsv"), source_row=row, sample="SYNTHETIC",
        symbol="TESTGENE", hgvsc=f"NM_999999.1:c.{row}G>A",
    )


class RecordingService:
    """Apply the production service's completed-source filter to each call."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def search_variants(self, variants, databases, artifact_root, **kwargs):
        pending = [
            (BrowserReviewService.variant_key(item), database)
            for item in variants for database in databases
            if (BrowserReviewService.variant_key(item), database)
            not in kwargs["completed_sources"]
        ]
        self.calls.append(pending)
        result = {}
        for key, database in pending:
            result.setdefault(key, []).append(self.outcomes[key, database])
        return result


@pytest.mark.parametrize(
    "protected_status",
    ["login_required", "layout_changed", "identity_mismatch", "ambiguous"],
)
def test_automatic_retry_runs_only_the_failed_variant_source_pairs(
    qt_app, tmp_path, protected_status,
):
    first, second = variant(10), variant(20)
    first_key, second_key = map(BrowserReviewService.variant_key, (first, second))
    protected = DatabaseEvidence("COSMIC", protected_status, "Needs review")
    outcomes = {
        (first_key, "COSMIC"): protected,
        (first_key, "OncoKB"): DatabaseEvidence("OncoKB", "error", "Slow page"),
        (second_key, "COSMIC"): DatabaseEvidence("COSMIC", "timeout", "Slow page"),
        (second_key, "OncoKB"): DatabaseEvidence("OncoKB", "found", "Verified"),
    }
    service = RecordingService(outcomes)
    worker = BrowserReviewWorker(
        [first, second], ["COSMIC", "OncoKB"], tmp_path, AppSettings(),
    )

    result = worker._search_patient_with_retries(
        service, "SYNTHETIC", [first, second], 1, "Synthetic patient",
    )

    assert service.calls[1] == [(first_key, "OncoKB"), (second_key, "COSMIC")]
    assert next(item for item in result[first_key] if item.database == "COSMIC") is protected
    assert len(service.calls) == 2


@pytest.mark.parametrize("kind", ["layout_changed", "login_required", "identity_mismatch", "ambiguous"])
def test_error_with_nontransient_failure_kind_is_not_automatically_retried(
    qt_app, tmp_path, kind,
):
    item = variant(10)
    key = BrowserReviewService.variant_key(item)
    service = RecordingService({
        (key, "OncoKB"): DatabaseEvidence("OncoKB", "error", raw={"failure_kind": kind}),
    })
    worker = BrowserReviewWorker([item], ["OncoKB"], tmp_path, AppSettings())

    worker._search_patient_with_retries(service, "SYNTHETIC", [item], 1, "Synthetic patient")

    assert len(service.calls) == 1


def test_stop_requested_after_first_pass_prevents_automatic_retry(
    qt_app, tmp_path, monkeypatch,
):
    item = variant(10)
    key = BrowserReviewService.variant_key(item)
    stopped = False

    class Service(RecordingService):
        def search_variants(self, *args, **kwargs):
            nonlocal stopped
            result = super().search_variants(*args, **kwargs)
            stopped = True
            return result

    service = Service({(key, "OncoKB"): DatabaseEvidence("OncoKB", "timeout")})
    worker = BrowserReviewWorker([item], ["OncoKB"], tmp_path, AppSettings())
    monkeypatch.setattr(worker, "_stop_requested", lambda: stopped)

    with pytest.raises(BrowserReviewCancelled):
        worker._search_patient_with_retries(service, "SYNTHETIC", [item], 1, "Synthetic patient")

    assert len(service.calls) == 1
