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
    headers = [
        window.evidence_table.horizontalHeaderItem(index).text()
        for index in range(window.evidence_table.columnCount())
    ]
    assert headers == ["Sample", "Gene", "HGVSc", *window.databases]


def test_database_diagnostics_cover_token_and_manual_statuses(qt_app):
    window = MainWindow()
    diagnostics = DatabaseSearchService(window.settings).database_diagnostics(window.databases)

    assert diagnostics["MTBP"] == "manual"
    assert diagnostics["HSMD"] == "manual"
    assert diagnostics["OncoKB"] in {"token required", "ready"}
    assert diagnostics["Franklin"] in {"token required", "ready"}
    assert diagnostics["COSMIC"] == "ready (basic/public lookup)"
    assert diagnostics["CIViC"] == "ready (open GraphQL)"
    assert diagnostics["CancerMine"] == "ready (cached cancer gene roles)"
    assert diagnostics["DGIdb"] == "context only (drug-gene, not MTB evidence)"
    assert diagnostics["ClinGen Allele Registry"] == "context only (allele ID/dbSNP cross-links)"
    assert diagnostics["cBioPortal"] == "ready (public cohort context)"
    assert diagnostics["gnomAD"].startswith("ready")
