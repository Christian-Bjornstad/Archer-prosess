from archer_processor.gui.app import MainWindow
from archer_processor.services import DatabaseSearchService


def test_database_tab_contains_current_sources(qt_app):
    window = MainWindow()

    assert window.databases == ["ClinVar", "MTBP", "HSMD", "COSMIC", "OncoKB", "Franklin", "gnomAD"]
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
    assert diagnostics["gnomAD"].startswith("ready")
