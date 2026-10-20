from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from archer_processor.gui.status_model import PatientStatusRow
from archer_processor.gui.widgets.status_matrix import display_status_label


class PatientDetailsDialog(QDialog):
    retry_requested = pyqtSignal(str)

    def __init__(self, row: PatientStatusRow, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.patient_id = row.patient_id
        self.setWindowTitle(f"{self.patient_id} · kildeoppslag")
        self.resize(640, 440)
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.heading)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        note = QLabel(
            "Ingen treff beskriver søkeresultatet. Lagrede oppslag beholdes når du prøver uferdige oppslag igjen."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.retry_button = QPushButton("Prøv feilede oppslag for denne pasienten")
        self.retry_button.clicked.connect(self._retry)
        layout.addWidget(self.retry_button)
        close = QPushButton("Lukk")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        self.set_patient(row)

    def set_patient(self, row: PatientStatusRow | None, *, retry_enabled: bool = False) -> None:
        if row is None:
            self.heading.setText(f"{self.patient_id} · ikke lenger i denne analysen")
            self.details.clear()
            self.retry_button.setEnabled(False)
            return
        self.heading.setText(f"{self.patient_id} · {row.variant_count} varianter")
        lines = [
            f"{database}: {display_status_label(cell.label)}"
            + (f"\n{cell.detail}" if cell.detail else "")
            for database, cell in row.cells.items()
        ]
        self.details.setPlainText("\n\n".join(lines))
        self.retry_button.setEnabled(retry_enabled)

    def _retry(self) -> None:
        self.accept()
        self.retry_requested.emit(self.patient_id)
