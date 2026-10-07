"""Render synthetic workstation previews without contacting evidence providers."""
from __future__ import annotations

import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication

from archer_processor.core.models import DatabaseEvidence, ProcessingResult, VariantRecord
from archer_processor.gui.app import MainWindow
from archer_processor.services.browser_sessions import BrowserSessionStatus
from archer_processor.services.settings import AppSettings


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tmp" / "workstation-preview"
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")
    for name in ("segoeui.ttf", "segoeuib.ttf"):
        QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / name))
    # Do not load recent clinical paths or write the workstation's configuration.
    AppSettings.load = classmethod(lambda cls: cls(default_output_dir=str(output)))
    window = MainWindow()
    variants = [
        VariantRecord(Path("synthetic.tsv"), index, f"DEMO-{patient:02}_VPM_A", gene, change)
        for index, (patient, gene, change) in enumerate([
            (1, "TP53", "c.524G>A"), (1, "CBL", "c.1203C>G"),
            (2, "ASXL1", "c.1934dup"), (3, "JAK2", "c.1849G>T"),
        ], start=2)
    ]
    for width, height in ((920, 600), (1024, 640), (1440, 900)):
        window.resize(width, height)
        window._switch_page(0)
        window.show()
        app.processEvents()
        window.grab().save(str(output / f"import-{width}.png"))
        window._set_import_mode(1)
        app.processEvents()
        window.grab().save(str(output / f"resume-{width}.png"))
        window._set_import_mode(0)

    window.result = ProcessingResult(Path("synthetic.tsv"), output / "DEMO_VPM_review.xlsx", "2026-10-07", variants, [])
    window.evidence = {
        f"{item.sample}|{item.hgvsc}": [DatabaseEvidence("ClinVar", "not_found", "Synthetic demonstration only")]
        for item in variants[:2]
    }
    statuses = [BrowserSessionStatus(name, "public" if name == "ClinVar" else "authenticated", "Synthetic demonstration only")
                for name in window.databases]
    for status in statuses:
        window._session_checked(status)
    window._session_check_finished(statuses)
    window._prepare_search_status(variants, window.databases, set())
    window._set_busy("Searching")
    window._source_state_changed("DEMO-01", "Franklin", True)
    window._source_state_changed("DEMO-01", "COSMIC", True)
    window._update_run_progress(0, 3, "DEMO-01 · 2 varianter · 5 kilder")
    window._search_started_at = 1.0
    window._log("Franklin: checking the exact variant before evidence capture")
    for width, height in ((920, 600), (1024, 640), (1440, 900)):
        window.resize(width, height)
        window._switch_page(1)
        window.database_scroll.verticalScrollBar().setValue(0)
        app.processEvents()
        window.grab().save(str(output / f"evidence-{width}.png"))
        window.database_scroll.ensureWidgetVisible(window.status_matrix, 0, 12)
        app.processEvents()
        window.grab().save(str(output / f"evidence-patients-{width}.png"))
        window.database_scroll.verticalScrollBar().setValue(window.database_scroll.verticalScrollBar().maximum())
        app.processEvents()
        window.grab().save(str(output / f"evidence-controls-{width}.png"))
    window._set_ready()
    window.run_progress.hide()
    window.artifact_path_edit.setText(str(ROOT / "reference_lists" / "Artefaktliste.xlsx"))
    window.who_genes_edit.setText(str(ROOT / "reference_lists" / "WHO-drivergener.xlsx"))
    window._validate_artifact_path()
    window._validate_who_path()
    for width, height in ((920, 600), (1024, 640), (1440, 900)):
        window.resize(width, height)
        window._switch_page(2)
        window.settings_scroll.verticalScrollBar().setValue(0)
        app.processEvents()
        window.grab().save(str(output / f"settings-{width}.png"))
    window.close()
    print(output)


if __name__ == "__main__":
    main()
