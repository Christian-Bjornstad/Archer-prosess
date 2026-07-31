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
    assert window.browser_signin_btn.isEnabled()
    assert not window.browser_review_btn.isEnabled()
    headers = [
        window.evidence_table.horizontalHeaderItem(index).text()
        for index in range(window.evidence_table.columnCount())
    ]
    assert headers == ["Sample", "Gene", "HGVSc", *window.databases]


def test_database_diagnostics_cover_token_and_manual_statuses(qt_app):
    window = MainWindow()
    diagnostics = DatabaseSearchService(window.settings).database_diagnostics(window.databases)

    assert diagnostics["MTBP"] == "web batch (login, research-only)"
    assert diagnostics["HSMD"] == "manual"
    assert diagnostics["OncoKB"] in {"token required", "ready"}
    assert diagnostics["Franklin"] in {
        "public web review (Premium API required for automation)",
        "ready",
        "ready (login on search)",
    }
    assert diagnostics["COSMIC"] == "ready (basic/public lookup)"
    assert diagnostics["CIViC"] == "ready (open GraphQL)"
    assert diagnostics["CancerMine"] == "ready (cached cancer gene roles)"
    assert diagnostics["DGIdb"] == "context only (drug-gene, not MTB evidence)"
    assert diagnostics["ClinGen Allele Registry"] == "context only (allele ID/dbSNP cross-links)"
    assert diagnostics["cBioPortal"] == "ready (public cohort context)"
    assert diagnostics["gnomAD"].startswith("ready")
