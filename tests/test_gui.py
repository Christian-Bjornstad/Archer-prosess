from pathlib import Path

from archer_processor.core import DatabaseEvidence, VariantProcessor
from archer_processor.gui.app import MainWindow
from archer_processor.services import DatabaseSearchService


def test_database_tab_contains_current_sources(qt_app):
    window = MainWindow()

    assert window.databases == [
        "ClinVar",
        "gnomAD",
        "COSMIC",
        "CIViC",
        "CancerMine",
        "DGIdb",
        "ClinGen Allele Registry",
        "cBioPortal",
        "MTBP",
        "HSMD",
        "OncoKB",
        "Franklin",
    ]
    assert set(window.db_checks) == set(window.databases)
    assert window.browser_database_combo.count() == 3
    assert window.browser_database_combo.itemText(0) == "OncoKB"
    assert window.browser_database_combo.itemText(2) == "MTBP"
    assert window.mtbp_cancer_type_edit.text() == window.settings.mtbp_cancer_type
    assert window.oncokb_password_edit.echoMode().name == "Password"
    assert window.franklin_password_edit.echoMode().name == "Password"
    assert window.mtbp_password_edit.echoMode().name == "Password"
    assert window.browser_delay_spin.value() == window.settings.browser_delay_seconds
    assert window.mtbp_timeout_spin.value() == window.settings.mtbp_timeout_minutes
    assert window.included_only_check.isChecked() == window.settings.search_included_only
    assert window.settings_scroll.widgetResizable()
    assert window.browser_signin_btn.isEnabled()
    assert not window.browser_review_btn.isEnabled()
    assert not window.patient_pdf_btn.isEnabled()
    headers = [
        window.evidence_table.horizontalHeaderItem(index).text()
        for index in range(window.evidence_table.columnCount())
    ]
    assert headers == ["Sample", "Gene", "HGVSc", *window.databases]


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


def test_normal_search_routes_non_api_login_sources_to_browser_phase(qt_app):
    window = MainWindow()
    for database in ["OncoKB", "Franklin", "MTBP"]:
        window.db_checks[database].setChecked(True)
    window.settings.oncokb_api_key = ""
    window.settings.franklin_api_key = ""

    assert window._selected_browser_databases(api_fallback_only=True) == [
        "OncoKB",
        "Franklin",
        "MTBP",
    ]

    window.settings.oncokb_api_key = "oncokb-token"
    window.settings.franklin_api_key = "franklin-token"
    assert window._selected_browser_databases(api_fallback_only=True) == ["MTBP"]


def test_database_diagnostics_cover_token_and_manual_statuses(qt_app):
    window = MainWindow()
    diagnostics = DatabaseSearchService(window.settings).database_diagnostics(window.databases)

    assert diagnostics["MTBP"] == "web batch (login, research-only)"
    assert diagnostics["HSMD"] == "manual"
    assert diagnostics["OncoKB"] in {"token required", "ready"}
    assert diagnostics["Franklin"] in {
        "browser login/public review (Premium API not configured)",
        "ready",
    }
    assert diagnostics["COSMIC"] == "ready (basic/public lookup)"
    assert diagnostics["CIViC"] == "ready (open GraphQL)"
    assert diagnostics["CancerMine"] == "ready (cached cancer gene roles)"
    assert diagnostics["DGIdb"] == "context only (drug-gene, not MTB evidence)"
    assert diagnostics["ClinGen Allele Registry"] == "context only (allele ID/dbSNP cross-links)"
    assert diagnostics["cBioPortal"] == "ready (public cohort context)"
    assert diagnostics["gnomAD"].startswith("ready")
