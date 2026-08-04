from pathlib import Path

from archer_processor.core import DatabaseEvidence, VariantProcessor
from archer_processor.gui.app import DatabaseWorker, MainWindow
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
    assert window.included_only_check.isChecked() == window.settings.search_included_only
    assert window.settings_scroll.widgetResizable()
    assert window.database_scroll.widgetResizable()
    assert window.browser_signin_btn.isEnabled()
    assert not window.browser_review_btn.isEnabled()
    assert not window.patient_excel_btn.isEnabled()
    assert not window.patient_pdf_btn.isEnabled()
    headers = [
        window.evidence_table.horizontalHeaderItem(index).text()
        for index in range(window.evidence_table.columnCount())
    ]
    assert headers == ["Sample", "Gene", "HGVSc", *window.databases]


def test_sidebar_navigation_switches_workspace_pages(qt_app):
    window = MainWindow()

    assert len(window.nav_buttons) == 4
    assert window.nav_buttons[0].isChecked()
    assert window.tabs.currentIndex() == 0

    window._switch_page(2)

    assert window.tabs.currentIndex() == 2
    assert window.nav_buttons[2].isChecked()
    assert window.page_title.text() == "Evidence workspace"
    assert window.page_eyebrow.text().endswith("EVIDENCE")


def test_review_filters_and_search_progress_are_visible(qt_app, tmp_path):
    window = MainWindow()
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window.result = VariantProcessor().process(
        fixture, "2026-08-01", tmp_path / "review.xlsx"
    )
    window._refresh_variant_table()

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


def test_application_icon_is_packaged_and_loaded(qt_app):
    window = MainWindow()

    assert window.app_icon_path.exists()
    assert not window.windowIcon().isNull()


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
        "ClinVar",
        "MTBP",
    ]


def test_database_diagnostics_cover_token_and_manual_statuses(qt_app):
    window = MainWindow()
    diagnostics = DatabaseSearchService(window.settings).database_diagnostics(window.databases)

    assert diagnostics["MTBP"] == "web batch (login, research-only)"
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

        def search_variants(self, patient_variants, databases, artifact_root, *, progress):
            patient_id = patient_variants[0].patient_id
            results = {}
            for database in databases:
                events.append((patient_id, database))
                for variant in patient_variants:
                    key = f"{variant.sample}|{variant.hgvsc}"
                    results.setdefault(key, []).append(
                        DatabaseEvidence(database, "found", "test")
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
    assert len(slept) == 1
    assert 10 <= slept[0] <= 20
