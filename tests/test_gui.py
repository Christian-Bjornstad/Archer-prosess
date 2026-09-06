from datetime import datetime
from pathlib import Path
import re
import time

from PIL import Image
from PyQt6.QtWidgets import QPushButton

from archer_processor.core import DatabaseEvidence, VariantProcessor, default_artifact_rules
from archer_processor.gui.app import (
    BrowserReviewWorker,
    DatabaseWorker,
    MainWindow,
    PatientReportWorker,
    _completed_evidence_sources,
    _protected_remote_evidence_sources,
)
from archer_processor.gui.status_model import (
    CellState,
    PatientStatusRow,
    RunPhase,
    RunActivity,
    RunSnapshot,
    StatusCell,
)
from archer_processor.gui.widgets.status_matrix import StatusMatrix
from archer_processor.gui.widgets.run_status import RunStatusStrip
from archer_processor.reports import ExcelReportWriter, PatientReportOutcome
from archer_processor.services import DatabaseSearchService
from archer_processor.services import AppSettings


def test_primary_and_secondary_text_actions_are_at_least_44px(qt_app):
    window = MainWindow()
    text_actions = [
        button
        for button in window.findChildren(QPushButton)
        if button.text() and button.objectName() != "SidebarButton"
    ]

    assert all(button.minimumHeight() >= 44 for button in text_actions)
    assert window.patient_excel_btn.text() == "Generer VEDLEGG_APP"
    assert window.patient_excel_btn.parent().objectName() == "ReportsGroup"


def test_evidence_actions_fit_target_window_sizes(qt_app):
    window = MainWindow()
    window._switch_page(2)
    window.show()
    for width, height in [(1120, 720), (1440, 900)]:
        window.resize(width, height)
        qt_app.processEvents()
        viewport = window.database_scroll.viewport().rect()
        for button in [
            window.rewrite_btn,
            window.patient_excel_btn,
        ]:
            point = button.mapTo(window.database_scroll.viewport(), button.rect().topLeft())
            assert point.x() >= 0
            assert point.x() + button.width() <= viewport.width()


def test_entire_evidence_page_scrolls_and_import_keeps_log(qt_app):
    window = MainWindow()
    window._switch_page(2)
    window.resize(1120, 720)
    window.show()
    qt_app.processEvents()
    before = window.search_btn.mapTo(window, window.search_btn.rect().topLeft())
    window.database_scroll.verticalScrollBar().setValue(window.database_scroll.verticalScrollBar().maximum())
    qt_app.processEvents()
    after = window.search_btn.mapTo(window, window.search_btn.rect().topLeft())
    assert after.y() < before.y()
    assert not hasattr(window, "evidence_splitter")
    assert not hasattr(window, "evidence_log")
    assert window.database_scroll.geometry().top() == 0
    assert window.database_scroll.geometry().bottom() >= window.database_scroll.parentWidget().height() - 2
    window._log('Test log message')
    assert 'Test log message' in window.log.toPlainText()
    window.close()


def test_import_fields_do_not_overlap_in_short_window(qt_app):
    window = MainWindow()
    window.resize(1120, 720)
    window.show()
    qt_app.processEvents()
    previous_bottom = -1
    for control in (window.input_edit, window.output_edit, window.run_date):
        top = control.mapTo(window.import_scroll.widget(), control.rect().topLeft()).y()
        assert control.height() >= 44
        assert top > previous_bottom
        previous_bottom = top + control.height()
    assert window.log.height() >= 120
    assert window.import_scroll.verticalScrollBar().maximum() > 0
    window.close()


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
    assert not hasattr(window, "evidence_table")
    assert not hasattr(window, "worker_count")
    assert not hasattr(window, "browser_security_label")


def test_artifact_settings_show_catalog_and_af_exception(qt_app):
    window = MainWindow()
    window._load_artifact_table(default_artifact_rules())

    headers = [
        window.artifact_table.horizontalHeaderItem(index).text()
        for index in range(window.artifact_table.columnCount())
    ]

    assert window.artifact_table.rowCount() == 39
    assert headers == ["Gene", "HGVSc", "Artifact through AF", "Reason"]
    assert window.artifact_table.item(0, 1).text() == "NM_015338.5:c.1934dup"
    assert window.artifact_table.item(0, 2).text() == "5.5%"
    assert window._artifact_rules_from_table() == default_artifact_rules()


def test_settings_are_grouped_into_four_operator_sections(qt_app):
    window = MainWindow()

    assert [group.title() for group in window.settings_groups] == [
        "Local files",
        "Browser access",
        "Search pacing",
        "Artifact rules",
    ]
    assert not hasattr(window, "history_edit")


def test_variant_workspace_has_no_external_history_column(qt_app):
    window = MainWindow()

    headers = [
        window.variant_table.horizontalHeaderItem(column).text()
        for column in range(window.variant_table.columnCount())
    ]

    assert headers == [
        "Sample",
        "Gene",
        "HGVSc",
        "AF",
        "Depth",
        "Decision",
        "Warnings",
    ]


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


def test_navigation_uses_short_labels_without_numbered_workflow_copy(qt_app):
    window = MainWindow()

    labels = [button.text() for button in window.navigation.buttons]

    assert labels == ["Import", "Variants", "Evidence", "Settings"]
    assert all(not re.match(r"\d", label) for label in labels)


def test_run_status_strip_exposes_interrupted_recovery_action(qt_app):
    strip = RunStatusStrip()
    strip.set_snapshot(
        RunSnapshot(
            phase=RunPhase.INTERRUPTED,
            current_patient=7,
            patient_total=28,
            patient_id="SYNTHETIC07",
        )
    )

    assert strip.phase_label.text() == "Interrupted · resume available"
    assert "7 / 28" in strip.progress_label.text()
    assert not strip.resume_button.isHidden()


def test_status_matrix_renders_visible_text_for_every_state(qt_app):
    matrix = StatusMatrix(["ClinVar", "Franklin"])
    matrix.set_rows(
        [
            PatientStatusRow(
                "SYNTHETIC01",
                2,
                {
                    "ClinVar": StatusCell(CellState.COMPLETE, "Complete"),
                    "Franklin": StatusCell(CellState.RETRY, "Retry"),
                    "Report": StatusCell(CellState.SAVE_PENDING, "Save pending"),
                },
            )
        ]
    )

    assert matrix.item(0, 2).text() == "Complete"
    assert matrix.item(0, 3).text() == "Retry"
    assert matrix.item(0, 4).text() == "Save pending"


def test_activity_updates_current_provider_and_timeline(qt_app):
    window = MainWindow()

    window._activity_received(
        RunActivity(
            datetime.now(),
            "SYNTHETIC02",
            "Franklin",
            "TP53 c.524G>A",
            "Capturing",
            "Population frequencies",
        )
    )

    assert window.current_activity.provider_value.text() == "Franklin"
    assert window.current_activity.patient_value.text() == "SYNTHETIC02"
    assert "Population frequencies" in window.log.toPlainText()


def test_locked_report_exposes_retry_action(qt_app, tmp_path):
    window = MainWindow()
    window.report_outcomes = {
        "SYNTHETIC02": PatientReportOutcome(
            "SYNTHETIC02",
            tmp_path / "SYNTHETIC02_VPM_Tolkning.xlsx",
            "locked",
            "locked",
        )
    }

    window._refresh_operations_cockpit()

    assert not window.retry_report_saves_button.isHidden()


def test_selected_patient_ids_use_all_patients_without_selection(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-01", tmp_path / "review.xlsx"
    )
    window._refresh_operations_cockpit()

    expected = sorted({variant.patient_id for variant in window.result.variants})
    assert window._selected_patient_ids() == expected


def test_selected_patient_ids_deduplicate_selected_rows(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-01", tmp_path / "review.xlsx"
    )
    window._refresh_operations_cockpit()
    first_patient = window.status_matrix.item(0, 0).text()
    window.status_matrix.selectRow(0)

    assert window._selected_patient_ids() == [first_patient]


def test_report_summary_uses_exact_norwegian_categories(qt_app, tmp_path):
    window = MainWindow()
    outcomes = [
        PatientReportOutcome("P1", tmp_path / "P1.xlsx", "created", ""),
        PatientReportOutcome("P2", tmp_path / "P2.xlsx", "updated", ""),
        PatientReportOutcome("P3", tmp_path / "P3.xlsx", "locked", "open"),
        PatientReportOutcome("P4", tmp_path / "P4.xlsx", "failed", "disk"),
    ]

    summary = window._report_outcome_summary(outcomes)

    assert summary == (
        "Opprettet: 1 · Oppdatert: 1 · "
        "Hoppet over/låst: 1 · Feilet: 1"
    )


def test_patient_report_worker_emits_progress_per_patient(qt_app, tmp_path):
    class Coordinator:
        @staticmethod
        def write_patient(patient_id):
            return PatientReportOutcome(
                patient_id, tmp_path / f"{patient_id}.xlsx", "created", ""
            )

    worker = PatientReportWorker(Coordinator(), ["P2", "P1"])
    progress = []
    finished = []
    worker.progress.connect(
        lambda current, total, detail: progress.append((current, total, detail))
    )
    worker.finished.connect(finished.append)

    worker.run()

    assert progress == [(1, 2, "P2"), (2, 2, "P1")]
    assert [outcome.patient_id for outcome in finished[0]] == ["P2", "P1"]


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


def test_variant_workspace_prioritises_toolbar_and_table(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-12", tmp_path / "review.xlsx"
    )
    for index, variant in enumerate(window.result.variants):
        variant.decision = "included" if index < 3 else "excluded"
    window._refresh_metrics()
    window._refresh_variant_table()

    assert window.variant_toolbar.objectName() == "VariantToolbar"
    assert window.variant_counters.text() == "5 total · 3 included · 2 excluded"
    assert window.variant_table.minimumHeight() >= 420
    assert not hasattr(window, "total_card")


def test_filtered_empty_state_explains_how_to_restore_rows(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-12", tmp_path / "review.xlsx"
    )
    window._refresh_variant_table()

    window.review_filter_edit.setText("NO_SUCH_VARIANT")

    assert not window.variant_empty_state.isHidden()
    assert "Clear filters" in window.variant_empty_state.text()


def test_variant_table_uses_distinct_strong_and_weak_germline_green(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-11", tmp_path / "review.xlsx"
    )
    window.result.variants[0].raw["Germ"] = 11
    window.result.variants[0].af = 0.35
    window.result.variants[0].artifact_status = ""
    window.result.variants[0].matched_rules = []
    window.result.variants[1].raw["Germ"] = 11
    window.result.variants[1].af = 0.3499
    window.result.variants[1].artifact_status = ""
    window.result.variants[1].matched_rules = []

    window._refresh_variant_table()

    assert window.variant_table.item(0, 0).background().color().name() == "#cdedd8"
    assert window.variant_table.item(1, 0).background().color().name() == "#e9f6ef"


def test_variant_table_uses_distinct_asxl1_artifact_oranges(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-09-03", tmp_path / "review.xlsx"
    )
    strong = window.result.variants[1]
    strong.af = 0.05
    strong.matched_rules = ["asxl1_1934dup_artifact"]
    light = window.result.variants[2]
    light.af = 0.0525
    light.matched_rules = ["asxl1_1934dup_artifact"]

    window._refresh_variant_table()

    assert window.variant_table.item(1, 0).background().color().name() == "#ffc000"
    assert window.variant_table.item(2, 0).background().color().name() == "#f4b183"


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
    saved = []
    monkeypatch.setattr(AppSettings, "save", lambda self: saved.append(True))

    window._load_processed_workbook(output)

    deadline = time.monotonic() + 5
    while window.result is None and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    for _ in range(10):
        qt_app.processEvents()
        time.sleep(0.01)

    assert window.result is not None
    assert window.result.total_count == result.total_count
    assert window.tabs.currentIndex() == 1
    assert window.status_badge.text() == "Workbook loaded"
    assert window.resume_edit.text() == str(output)
    assert "Restored 5 variants" in window.resume_status.text()
    assert not window.included_only_check.isChecked()
    assert window.settings.last_processed_workbook == str(output)
    assert saved == [True]
    assert messages[0][1] == "Analysis restored"


def test_startup_offers_recent_analysis_without_loading_or_searching(
    qt_app, tmp_path, monkeypatch
):
    workbook = tmp_path / "review.xlsx"
    workbook.write_bytes(b"synthetic")
    monkeypatch.setattr(
        "archer_processor.gui.app.AppSettings.load",
        lambda: AppSettings(last_processed_workbook=str(workbook)),
    )
    loads = []
    searches = []
    monkeypatch.setattr(
        MainWindow,
        "_load_processed_workbook",
        lambda *args: loads.append(args),
    )
    monkeypatch.setattr(
        DatabaseSearchService,
        "search_variant",
        lambda *args, **kwargs: searches.append((args, kwargs)),
    )

    window = MainWindow()

    assert not window.recent_analysis_panel.isHidden()
    assert window.recent_analysis_name.text() == "review.xlsx"
    assert loads == []
    assert searches == []


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


def test_browser_worker_passes_restored_evidence_to_provider_resume(
    qt_app, tmp_path
):
    variant = VariantProcessor().process(
        Path(__file__).parent / "fixtures" / "sample_variants.tsv",
        "2026-08-05",
        tmp_path / "resume.xlsx",
    ).variants[0]
    key = f"{variant.sample}|{variant.hgvsc}"
    prior = DatabaseEvidence(
        "MTBP", "timeout", "pending", raw={"analysis_id": "ARCHER-pending"}
    )
    worker = BrowserReviewWorker(
        [variant],
        ["MTBP"],
        tmp_path / "evidence",
        AppSettings(),
        existing_evidence={key: [prior]},
    )
    received = []

    class Service:
        def search_variants(self, *args, prior_evidence=None, **kwargs):
            received.append(prior_evidence)
            return {key: [prior]}

    worker._run_search_pass(Service(), variant.patient_id, [variant], 1, "Patient 1")

    assert received == [{key: [prior]}]


def test_resume_keeps_unverified_and_partial_evidence_pending():
    key = "SYNTHETIC_VPM_1|NM_000546.6:c.524G>A"
    for status in (
        "identity_mismatch",
        "partial_capture",
        "verification_required",
        "quota_exhausted",
        "session_lost",
    ):
        completed = _completed_evidence_sources(
            {key: [DatabaseEvidence("Franklin", status, "retry")]}
        )
        assert (key, "Franklin") not in completed


def test_worker_failure_preserves_retryable_mtbp_report_id():
    key = "SYNTHETIC_VPM_1|NM_000546.6:c.524G>A"
    evidence = {
        key: [
            DatabaseEvidence(
                "MTBP",
                "timeout",
                "pending",
                raw={"analysis_id": "ARCHER-pending"},
            )
        ]
    }

    assert (key, "MTBP") in _protected_remote_evidence_sources(evidence)


def test_application_icon_is_packaged_and_loaded(qt_app):
    window = MainWindow()

    assert window.app_icon_path.exists()
    assert window.app_icon_path.name == "vpm-tolkning-icon.png"
    assert window.windowTitle() == "VPM Tolkning"
    assert ":not(" not in window.styleSheet()
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
    result = VariantProcessor().process(
        fixture, "2026-08-01", tmp_path / "review.xlsx"
    )
    variants = result.variants[:2]
    events = []
    report_events = []
    restored_key = f"{variants[0].sample}|{variants[0].hgvsc}"
    restored_evidence = {
        restored_key: [
            DatabaseEvidence(
                "MTBP",
                "timeout",
                "pending",
                raw={"analysis_id": "ARCHER-restored"},
            )
        ]
    }
    prior_snapshots = []

    class FakeReportCoordinator:
        def __init__(self, result, variants, evidence):
            report_events.append("constructed")
            raise AssertionError("evidence searches must not coordinate patient reports")

        def merge(self, incoming):
            raise AssertionError("evidence searches must not merge patient reports")

        def reconcile(self):
            raise AssertionError("evidence searches must not write patient reports")

        def write_patient(self, patient_id):
            raise AssertionError("patient reports must not be written mid-search")

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
            prior_evidence=None,
        ):
            prior_snapshots.append(prior_evidence)
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
    monkeypatch.setattr(
        "archer_processor.gui.app.PatientReportCoordinator", FakeReportCoordinator
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
        result=result,
        existing_evidence=restored_evidence,
        report_variants=variants,
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
    assert report_events == []
    assert prior_snapshots == [restored_evidence, restored_evidence]


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
