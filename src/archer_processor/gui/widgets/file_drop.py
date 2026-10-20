from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout


class AnalysisFileDrop(QFrame):
    """Accept one local analysis file; opening remains an explicit app action."""

    file_selected = pyqtSignal(str)
    supported_suffixes = {".tsv", ".txt", ".xlsx"}

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("AnalysisFileDrop")
        self.setAcceptDrops(True)
        self.setAccessibleName("Drop one variant TSV or processed workbook here")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("Slipp filen her")
        title.setObjectName("SectionTitle")
        detail = QLabel("Ny analyse: TSV  ·  Fortsett analyse: VPM Excel-fil (.xlsx)")
        detail.setObjectName("HelperText")
        detail.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(detail)

    def _local_path(self, event: QDragEnterEvent | QDropEvent) -> Path | None:
        urls = event.mimeData().urls()
        if not self.isEnabled() or len(urls) != 1 or not urls[0].isLocalFile():
            return None
        path = Path(urls[0].toLocalFile())
        return path if path.suffix.casefold() in self.supported_suffixes and path.is_file() else None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._local_path(event) is not None:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        path = self._local_path(event)
        if path is None:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.file_selected.emit(str(path))
