from pathlib import Path
import re

from PIL import Image

from archer_processor.core import DatabaseEvidence, VariantProcessor, default_artifact_rules
from archer_processor.gui.app import DatabaseWorker, MainWindow
from archer_processor.reports import ExcelReportWriter
from archer_processor.services import DatabaseSearchService


def test_database_tab_contains_current_sources(qt_app):
    window = MainWindow()

    assert window.databases == [
        "MTBP",
        "Franklin",
        "ClinVar",
        "OncoKB",
        "COSMIC",
    ]
    assert set(window.db_checks) == set(window.databases)
    assert window.browser_database_combo.count() == 5
    assert window.browser_database_combo.itemText(0) == "COSMIC"
    assert window.browser_database_combo.itemText(3) == "ClinVar"
    assert window.browser_database_combo.itemText(4) == "MTBP"
    assert window.mtbp_cancer_type_edit.text() == window.settings.mtbp_cancer_type
    assert window.oncokb_password_edit.echoMode().name == "Password"
    assert window.cosmic_password_edit.echoMode().name == "Password"
    assert window.franklin_password_edit.echoMode().name == "Password"
    assert window.mtbp_password_edit.echoMode().name == "Password"
    assert window.browser_delay_spin.value() == window.settings.browser_delay_seconds
    assert (
        window.browser_delay_max_spin.value()
        == window.settings.browser_delay_max_seconds
    )
    assert window.mtbp_timeout_spin.value() == window.settings.mtbp_timeout_minutes
    assert (
        window.browser_background_check.isChecked()
        == window.settings.browser_background
    )
    assert window.included_only_check.isChecked() == window.settings.search_included_only
    assert window.settings_scroll.widgetResizable()
    assert window.database_scroll.widgetResizable()
    assert window.browser_signin_btn.isEnabled()
    assert not window.browser_review_btn.isEnabled()
    assert not window.stop_search_btn.isEnabled()
    assert window.stop_search_btn.text() == "Stop Search"
    assert not window.pause_search_btn.isEnabled()
    assert window.pause_search_btn.text() == "Pause Search"
    assert not window.patient_excel_btn.isEnabled()
    assert not hasattr(window, "patient_pdf_btn")
    assert not hasattr(window, "clinvar_key_edit")
    assert not hasattr(window, "oncokb_key_edit")
    assert not hasattr(window, "franklin_key_edit")
    headers = [
        window.evidence_table.horizontalHeaderItem(index).text()
        for index in range(window.evidence_table.columnCount())
    ]
    assert headers == ["Sample", "Gene", "HGVSc", *window.databases]


def test_artifact_settings_show_catalog_and_af_exception(qt_app):
    window = MainWindow()
    window._load_artifact_table(default_artifact_rules())

    headers = [
        window.artifact_table.horizontalHeaderItem(index).text()
        for index in range(window.artifact_table.columnCount())
    ]

    assert window.artifact_table.rowCount() == 36
    assert headers == ["Gene", "HGVSc", "Artifact through AF", "Reason"]
    assert window.artifact_table.item(0, 1).text() == "NM_015338.5:c.1934dup"
    assert window.artifact_table.item(0, 2).text() == "5.5%"
    assert window._artifact_rules_from_table() == default_artifact_rules()


def test_sidebar_navigation_switches_workspace_pages(qt_app):
    window = MainWindow()

    assert len(window.nav_buttons) == 4
    assert window.nav_buttons[0].isChecked()
    assert window.tabs.currentIndex() == 0

    window._switch_page(2)

    assert window.tabs.currentIndex() == 2
    assert window.nav_buttons[2].isChecked()
    assert window.page_title.text() == "Evidence search"
    assert window.page_eyebrow.text().endswith("EVIDENCE")


def test_review_filters_and_search_progress_are_visible(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-01", tmp_path / "review.xlsx"
    )
    window._refresh_variant_table()

    assert not hasattr(window, "review_flag_note")
    assert "Review flags" not in [
        window.review_decision_combo.itemText(index)
        for index in range(window.review_decision_combo.count())
    ]

    window.review_filter_edit.setText(window.result.variants[0].symbol)
    assert "Showing" in window.review_count_label.text()
    assert any(
        not window.variant_table.isRowHidden(row)
        for row in range(window.variant_table.rowCount())
    )

    window._update_run_progress(2, 5, "Patient 3 is running")
    assert not window.run_progress.isHidden()
    assert window.run_progress.bar.value() == 2
    assert window.run_progress.count.text() == "2 / 5 patients"
    assert window.run_progress.detail.text() == "Patient 3 is running"


def test_variant_table_uses_distinct_strong_and_weak_green(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-11", tmp_path / "review.xlsx"
    )
    window.result.variants[0].history_matches = [{"Tier I": 6}]
    window.result.variants[0].artifact_status = ""
    window.result.variants[0].matched_rules = []
    window.result.variants[1].history_matches = [{"Germ": 11}]
    window.result.variants[1].af = 0.3499
    window.result.variants[1].artifact_status = ""
    window.result.variants[1].matched_rules = []

    window._refresh_variant_table()

    assert window.variant_table.item(0, 0).background().color().name() == "#cdedd8"
    assert window.variant_table.item(1, 0).background().color().name() == "#e9f6ef"


def test_locked_workbook_shows_warning_without_raising(qt_app, tmp_path, monkeypatch):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-01", tmp_path / "review.xlsx"
    )
    warnings = []

    def locked_write():
        raise PermissionError(13, "Permission denied", str(window.result.output_path))

    monkeypatch.setattr(window, "_write_evidence_workbook", locked_write)
    monkeypatch.setattr(
        "archer_processor.gui.app.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    window._rewrite_workbook()

    assert window.workbook_write_pending
    assert len(warnings) == 1
    assert warnings[0][1] == "Workbook is open in Excel"
    assert "Evidence remains available in the app" in window.log.toPlainText()


def test_completed_search_status_remains_visible(qt_app):
    window = MainWindow()
    window._update_run_progress(2, 2, "Completed patient")

    window._complete_run_progress("Evidence search complete")

    assert not window.run_progress.isHidden()
    assert window.run_progress.title.text() == "Evidence search complete"
    assert "ready for review" in window.run_progress.detail.text()
    assert window.status_badge.text() == "Search complete"


def test_completed_search_reports_pending_workbook_save(qt_app):
    window = MainWindow()
    window.workbook_write_pending = True
    window._update_run_progress(1, 1, "Completed patient")

    window._complete_run_progress("Evidence search complete")

    assert "still open in Excel" in window.run_progress.detail.text()
    assert window.status_badge.text() == "Search complete · save pending"


def test_processed_workbook_can_resume_into_review_pages(qt_app, tmp_path, monkeypatch):
    output = tmp_path / "resume.xlsx"
    result = VariantProcessor().process(
        Path(__file__).parent / "fixtures" / "sample_variants.tsv",
        "2026-08-05",
        output,
    )
    ExcelReportWriter().write(result, output)
    messages = []
    monkeypatch.setattr(
        "archer_processor.gui.app.QMessageBox.information",
        lambda *args: messages.append(args),
    )
    window = MainWindow()

    window._load_processed_workbook(output)

    assert window.result is not None
    assert window.result.total_count == result.total_count
    assert window.tabs.currentIndex() == 1
    assert window.status_badge.text() == "Workbook loaded"
    assert window.resume_edit.text() == str(output)
    assert "Restored 5 variants" in window.resume_status.text()
    assert not window.included_only_check.isChecked()
    assert messages[0][1] == "Analysis restored"


def test_new_search_results_merge_with_restored_evidence(qt_app, tmp_path, monkeypatch):
    window = MainWindow()
    variant = VariantProcessor().process(
        Path(__file__).parent / "fixtures" / "sample_variants.tsv",
        "2026-08-05",
        tmp_path / "resume.xlsx",
    ).variants[0]
    key = f"{variant.sample}|{variant.hgvsc}"
    window.result = VariantProcessor().process(
        Path(__file__).parent / "fixtures" / "sample_variants.tsv",
        "2026-08-05",
        tmp_path / "resume.xlsx",
    )
    window.evidence = {key: [DatabaseEvidence("ClinVar", "found", "Existing")]}
    monkeypatch.setattr(window, "_auto_rewrite_workbook", lambda: None)

    window._database_finished(
        {key: [DatabaseEvidence("OncoKB", "found", "New result")]}
    )

    assert {item.database for item in window.evidence[key]} == {"ClinVar", "OncoKB"}


def test_application_icon_is_packaged_and_loaded(qt_app):
    window = MainWindow()

    assert window.app_icon_path.exists()
    assert window.app_icon_path.name == "vpm-tolkning-icon.png"
    assert window.windowTitle() == "VPM Tolkning"
    assert not window.windowIcon().isNull()
    with Image.open(window.app_icon_path) as icon:
        corners = [
            icon.getpixel((0, 0)),
            icon.getpixel((icon.width - 1, 0)),
            icon.getpixel((0, icon.height - 1)),
            icon.getpixel((icon.width - 1, icon.height - 1)),
        ]
    assert all(max(corner[:3]) < 80 for corner in corners)


def test_browser_evidence_merge_replaces_placeholders_for_every_variant(qt_app):
    window = MainWindow()
    window.evidence = {
        "patient-a|variant-a": [
            DatabaseEvidence("OncoKB", "token_required", "API placeholder")
        ],
        "patient-b|variant-b": [
            DatabaseEvidence("OncoKB", "token_required", "API placeholder")
        ],
    }

    window._merge_browser_evidence(
        {
            "patient-a|variant-a": [
                DatabaseEvidence("OncoKB", "found", "Browser result A")
            ],
            "patient-b|variant-b": [
                DatabaseEvidence("OncoKB", "found", "Browser result B")
            ],
        }
    )

    assert window.evidence["patient-a|variant-a"][0].summary == "Browser result A"
    assert window.evidence["patient-b|variant-b"][0].summary == "Browser result B"


def test_database_lookup_scope_can_be_limited_to_included_variants(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-01", tmp_path / "review.xlsx"
    )

    window.included_only_check.setChecked(True)
    assert window._variants_for_search() == window.result.included

    window.included_only_check.setChecked(False)
    assert window._variants_for_search() == window.result.variants

    skipped = window.result.variants[0]
    window.database_skip_keys = {f"{skipped.sample}|{skipped.hgvsc}"}
    assert skipped not in window._variants_for_search()
    assert len(window._variants_for_search()) == window.result.total_count - 1


def test_resume_scope_keeps_only_variants_with_unfinished_selected_sources(
    qt_app, tmp_path
):
    window = MainWindow()
    window.result = VariantProcessor().process(
        Path(__file__).parent / "fixtures" / "sample_variants.tsv",
        "2026-08-10",
        tmp_path / "review.xlsx",
    )
    window.included_only_check.setChecked(False)
    first, second = window.result.variants[:2]
    window.evidence = {
        f"{first.sample}|{first.hgvsc}": [
            DatabaseEvidence("ClinVar", "found", "complete"),
            DatabaseEvidence("OncoKB", "not_found", "complete"),
        ],
        f"{second.sample}|{second.hgvsc}": [
            DatabaseEvidence("ClinVar", "found", "complete"),
            DatabaseEvidence("OncoKB", "error", "retry this source"),
        ],
    }

    pending = window._pending_variants_for_search(["ClinVar", "OncoKB"])

    assert first not in pending
    assert second in pending


def test_log_lines_have_clock_timestamps_and_elapsed_search_time(
    qt_app, monkeypatch
):
    window = MainWindow()
    monkeypatch.setattr("archer_processor.gui.app.time.monotonic", lambda: 225.0)
    window._search_started_at = 100.0

    window._log(f"Search checkpoint ({window._search_elapsed_text()})")

    line = window.log.toPlainText().splitlines()[-1]
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}] ", line)
    assert "2m 05s" in line


def test_normal_search_routes_non_api_login_sources_to_browser_phase(qt_app):
    window = MainWindow()
    for check in window.db_checks.values():
        check.setChecked(False)
    for database in ["COSMIC", "OncoKB", "Franklin", "ClinVar", "MTBP"]:
        window.db_checks[database].setChecked(True)
    window.settings.oncokb_api_key = ""
    window.settings.franklin_api_key = ""

    assert window._selected_browser_databases(api_fallback_only=True) == [
        "COSMIC",
        "OncoKB",
        "Franklin",
        "ClinVar",
        "MTBP",
    ]

    window.settings.oncokb_api_key = "oncokb-token"
    window.settings.franklin_api_key = "franklin-token"
    assert window._selected_browser_databases(api_fallback_only=True) == [
        "COSMIC",
        "OncoKB",
        "Franklin",
        "ClinVar",
        "MTBP",
    ]


def test_database_diagnostics_cover_token_and_manual_statuses(qt_app):
    window = MainWindow()
    diagnostics = DatabaseSearchService(window.settings).database_diagnostics(window.databases)

    assert diagnostics["MTBP"] == "web one-variant reports (login, research-only)"
    assert diagnostics["OncoKB"] in {"token required", "ready"}
    assert diagnostics["Franklin"] in {
        "browser login/public review (Premium API not configured)",
        "ready",
    }
    assert diagnostics["COSMIC"].startswith("browser login")
    assert diagnostics["ClinVar"].startswith("browser summary capture")


def test_database_worker_completes_all_sources_before_next_patient(
    qt_app, tmp_path, monkeypatch
):
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    variants = VariantProcessor().process(
        fixture, "2026-08-01", tmp_path / "review.xlsx"
    ).variants[:2]
    events = []

    class FakeApiService:
        def __init__(self, settings):
            pass

        def database_diagnostics(self, databases):
            return {database: "ready" for database in databases}

        @staticmethod
        def variant_key(variant):
            return f"{variant.sample}|{variant.hgvsc}"

        def search_variant(self, variant, databases):
            database = list(databases)[0]
            events.append((variant.patient_id, database))
            return [DatabaseEvidence(database, "found", "test")]

    class FakeBrowserService:
        def __init__(self, **kwargs):
            pass

        def search_variants(
            self,
            patient_variants,
            databases,
            artifact_root,
            *,
            progress,
            completed_sources,
            checkpoint,
        ):
            patient_id = patient_variants[0].patient_id
            results = {}
            for database in databases:
                events.append((patient_id, database))
                for variant in patient_variants:
                    key = f"{variant.sample}|{variant.hgvsc}"
                    results.setdefault(key, []).append(
                        DatabaseEvidence(database, "found", "test")
                    )
                checkpoint(
                    {
                        f"{variant.sample}|{variant.hgvsc}": [
                            DatabaseEvidence(database, "found", "test")
                        ]
                        for variant in patient_variants
                    }
                )
            return results

    monkeypatch.setattr(
        "archer_processor.gui.app.DatabaseSearchService", FakeApiService
    )
    monkeypatch.setattr(
        "archer_processor.gui.app.BrowserReviewService", FakeBrowserService
    )
    settings = MainWindow().settings
    settings.browser_delay_seconds = 10
    settings.browser_delay_max_seconds = 20
    slept = []
    monkeypatch.setattr("archer_processor.gui.app.time.sleep", slept.append)
    worker = DatabaseWorker(
        variants,
        ["OncoKB"],
        ["COSMIC", "Franklin", "ClinVar", "MTBP"],
        tmp_path / "evidence",
        settings,
    )

    finished = []
    progress = []
    worker.finished.connect(finished.append)
    worker.progress.connect(lambda current, total, detail: progress.append((current, total, detail)))
    worker.run()

    assert events == [
        (variants[0].patient_id, "OncoKB"),
        (variants[0].patient_id, "COSMIC"),
        (variants[0].patient_id, "Franklin"),
        (variants[0].patient_id, "ClinVar"),
        (variants[0].patient_id, "MTBP"),
        (variants[1].patient_id, "OncoKB"),
        (variants[1].patient_id, "COSMIC"),
        (variants[1].patient_id, "Franklin"),
        (variants[1].patient_id, "ClinVar"),
        (variants[1].patient_id, "MTBP"),
    ]
    assert len(finished) == 1
    assert all(len(items) == 5 for items in finished[0].values())
    assert progress[0][:2] == (0, 2)
    assert progress[-1][:2] == (2, 2)
    # No API query is delayed; the only pause is before patient 2's website phase.
    assert 10 <= sum(slept) <= 20
    assert all(delay <= 0.25 for delay in slept)


def test_stop_search_requests_safe_interruption_and_keeps_status(
    qt_app, tmp_path, monkeypatch
):
    window = MainWindow()
    worker = DatabaseWorker([], [], [], tmp_path / "evidence", window.settings)
    worker.request_pause()

    class ActiveThread:
        def __init__(self):
            self.interrupted = False

        def isRunning(self):
            return True

        def requestInterruption(self):
            self.interrupted = True

    thread = ActiveThread()
    window.database_worker = worker
    window.database_thread = thread
    monkeypatch.setattr(window, "_auto_rewrite_workbook", lambda: None)

    window._set_busy("Searching")
    assert window.stop_search_btn.isEnabled()
    window._stop_evidence_search()

    assert thread.interrupted
    assert not worker.pause_control.pause_requested
    assert window.stop_search_btn.text() == "Stopping…"
    assert "already collected" in window.run_progress.detail.text()

    window._search_cancelled()

    assert not window.stop_search_btn.isEnabled()
    assert window.status_badge.text() == "Search stopped"
    assert "kept" in window.run_progress.detail.text()


def test_pause_and_resume_keep_the_same_active_search_queue(qt_app, tmp_path):
    window = MainWindow()
    worker = DatabaseWorker([], [], [], tmp_path / "evidence", window.settings)

    class ActiveThread:
        @staticmethod
        def isRunning():
            return True

    window.database_worker = worker
    window.database_thread = ActiveThread()
    window._set_busy("Searching")

    window._toggle_search_pause()

    assert worker.pause_control.pause_requested
    assert window.pause_search_btn.text() == "Resume Search"
    assert window.status_badge.text() == "Pausing"
    assert "current browser action" in window.run_progress.detail.text()

    window._toggle_search_pause()

    assert not worker.pause_control.pause_requested
    assert window.pause_search_btn.text() == "Pause Search"
    assert window.status_badge.text() == "Resuming"
