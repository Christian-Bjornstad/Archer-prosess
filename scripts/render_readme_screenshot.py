"""Render the actual Evidence UI with synthetic data; no website requests."""
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


def main():
    app = QApplication([])
    app.setStyle("Fusion")
    for filename in ("segoeui.ttf", "segoeuib.ttf"):
        font = Path("C:/Windows/Fonts") / filename
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    window = MainWindow()
    variants = [
        VariantRecord(Path("synthetic.tsv"), index, f"DEMO-{index:02}", gene, cdna)
        for index, (gene, cdna) in enumerate([
            ("CBL", "c.1203C>G"), ("TP53", "c.524G>A"), ("ASXL1", "c.1934dup"),
        ], start=1)
    ]
    window.result = ProcessingResult(Path("synthetic.tsv"), None, "2026-09-06", variants, [])
    window.evidence = {
        f"{variant.sample}|{variant.hgvsc}": [
            DatabaseEvidence(database, "found", "Synthetic demonstration only")
            for database in window.databases
        ] for variant in variants
    }
    for check in window.db_checks.values():
        check.setChecked(True)
    window._set_ready()
    window._refresh_operations_cockpit()
    window._update_evidence_summary()
    window._switch_page(2)
    window.resize(1320, 1120)
    window.show()
    app.processEvents()
    window.search_btn.clearFocus()
    window.setFocus()
    app.processEvents()
    output = ROOT / "docs/assets/vpm-tolkning-evidence.png"
    if not window.grab().save(str(output)):
        raise RuntimeError(f"Could not save {output}")
    print(output)
    window.close()


if __name__ == "__main__":
    main()
