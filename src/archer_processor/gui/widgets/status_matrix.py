from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtCore import QItemSelectionModel, QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from archer_processor.gui.status_model import CellState, PatientStatusRow


_STATE_COLORS = {
    CellState.COMPLETE: ("#E9F6EF", "#18794E"),
    CellState.REPORT_SAVED: ("#E9F6EF", "#18794E"),
    CellState.RUNNING: ("#E7F4F7", "#087EA4"),
    CellState.RETRY: ("#FFF5D6", "#714600"),
    CellState.MANUAL_REVIEW: ("#FFF0E6", "#9A3412"),
    CellState.SAVE_PENDING: ("#FFF5D6", "#714600"),
    CellState.NOT_FOUND: ("#F1F5F7", "#516875"),
    CellState.STOPPED: ("#F8E8E8", "#B42318"),
    CellState.QUEUED: ("#F1F5F7", "#516875"),
    CellState.SKIPPED: ("#F1F5F7", "#516875"),
    CellState.NOT_SELECTED: ("#F1F5F7", "#516875"),
    CellState.NOT_READY: ("#F1F5F7", "#516875"),
}


_DISPLAY_LABELS = {
    "Running": "Søker", "Queued": "Venter", "Retry": "Prøv igjen",
    "Error": "Feil", "Not found": "Ingen treff", "Complete": "Utført",
    "Not selected": "Ikke valgt", "Skipped": "Hoppet over",
    "Manual review": "Manuell vurdering", "Stopped": "Stoppet",
    "Report saved": "Vedlegg lagret", "Save pending": "Lagring venter",
    "Not ready": "Ikke klar",
}


def display_status_label(label: str) -> str:
    """Translate display text while preserving counts and clinical state values."""
    for source, translated in _DISPLAY_LABELS.items():
        if label == source or label.startswith(source + " "):
            return translated + label[len(source):]
    return label


class StatusMatrix(QTableWidget):
    cell_activated = pyqtSignal(str, str)
    patient_selection_changed = pyqtSignal()
    patient_focused = pyqtSignal(str)
    patient_details_changed = pyqtSignal(str)

    def __init__(self, databases: Sequence[str]) -> None:
        self.databases = list(databases)
        super().__init__(0, 3 + len(self.databases))
        self.setObjectName("PatientStatusMatrix")
        self.setHorizontalHeaderLabels(
            ["Pasient", "Varianter", *self.databases, "Vedlegg"]
        )
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setMinimumSectionSize(64)
        self.horizontalHeader().setStretchLastSection(True)
        self.setColumnWidth(0, 160)
        self.setColumnWidth(1, 80)
        for column in range(2, self.columnCount()):
            self.setColumnWidth(column, 128)
        self.setColumnWidth(self.columnCount() - 1, 132)
        self.itemActivated.connect(self._emit_activation)
        self._rows: tuple[PatientStatusRow, ...] = ()
        self._checked_patients: set[str] = set()
        self._focused_patient_id: str | None = None
        self.itemChanged.connect(self._patient_check_changed)
        self.currentItemChanged.connect(self._notify_patient_focus)

    def selected_patients(self) -> list[str]:
        """Return checked patient IDs in visible order, independently of focus."""
        return [row.patient_id for row in self._rows if row.patient_id in self._checked_patients]

    def set_selected_patients(self, patient_ids: Sequence[str]) -> None:
        requested = set(patient_ids).intersection(row.patient_id for row in self._rows)
        if requested == self._checked_patients:
            return
        blocker = QSignalBlocker(self)
        try:
            self._checked_patients = requested
            for row, row_data in enumerate(self._rows):
                self.item(row, 0).setCheckState(
                    Qt.CheckState.Checked if row_data.patient_id in requested
                    else Qt.CheckState.Unchecked
                )
        finally:
            blocker.unblock()
        self.patient_selection_changed.emit()

    def select_all_patients(self) -> None:
        self.set_selected_patients([row.patient_id for row in self._rows])

    def unselect_all_patients(self) -> None:
        self.set_selected_patients([])

    def focused_patient(self) -> str | None:
        item = self.item(self.currentRow(), 0) if self.currentRow() >= 0 else None
        return item.text() if item is not None else None

    def patient_details(self, patient_id: str | None = None) -> PatientStatusRow | None:
        patient_id = patient_id if patient_id is not None else self.focused_patient()
        for row in self._rows:
            if row.patient_id == patient_id:
                return PatientStatusRow(row.patient_id, row.variant_count, dict(row.cells))
        return None

    def _patient_check_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        patient_id = item.data(Qt.ItemDataRole.UserRole)
        checked = item.checkState() == Qt.CheckState.Checked
        if checked == (patient_id in self._checked_patients):
            return
        if checked:
            self._checked_patients.add(patient_id)
        else:
            self._checked_patients.discard(patient_id)
        self.patient_selection_changed.emit()

    def _notify_patient_focus(self, *_args) -> None:
        patient_id = self.focused_patient()
        if patient_id != self._focused_patient_id:
            self._focused_patient_id = patient_id
            self.patient_focused.emit(patient_id or "")
            self.patient_details_changed.emit(patient_id or "")

    def set_rows(self, rows: Sequence[PatientStatusRow]) -> None:
        previous_details = self.patient_details()
        previous_checked = self._checked_patients.copy()
        blocker = QSignalBlocker(self)
        updates_enabled = self.updatesEnabled()
        vertical_position = self.verticalScrollBar().value()
        horizontal_position = self.horizontalScrollBar().value()
        selected_patients = {
            self.item(index.row(), 0).text()
            for index in self.selectionModel().selectedRows()
            if self.item(index.row(), 0) is not None
        }
        current_patient = (
            self.item(self.currentRow(), 0).text()
            if self.currentRow() >= 0 and self.item(self.currentRow(), 0) is not None
            else None
        )
        current_column = self.currentColumn()
        previous_order = [row.patient_id for row in self._rows]
        new_order = [row.patient_id for row in rows]
        self._checked_patients.intersection_update(new_order)
        self.setUpdatesEnabled(False)
        try:
            if self.rowCount() != len(rows):
                self.setRowCount(len(rows))
            for row, row_data in enumerate(rows):
                if row < len(self._rows) and self._rows[row] == row_data:
                    continue
                patient_item = self._set_item(row, 0, row_data.patient_id, row_data.patient_id)
                patient_item.setFlags(patient_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                patient_item.setCheckState(
                    Qt.CheckState.Checked if row_data.patient_id in self._checked_patients
                    else Qt.CheckState.Unchecked
                )
                patient_item.setToolTip("Velg pasient for søk eller vedlegg. Klikk raden for detaljer.")
                self._set_item(row, 1, str(row_data.variant_count))
                for column, database in enumerate([*self.databases, "Report"], start=2):
                    cell = row_data.cells[database]
                    item = self._set_item(
                        row, column, display_status_label(cell.label), (row_data.patient_id, database)
                    )
                    tooltip = cell.detail or cell.label
                    if item.toolTip() != tooltip:
                        item.setToolTip(tooltip)
                    background, foreground = map(QColor, _STATE_COLORS[cell.state])
                    if item.background().color() != background:
                        item.setBackground(background)
                    if item.foreground().color() != foreground:
                        item.setForeground(foreground)

            if previous_order != new_order:
                self.clearSelection()
                for row, patient_id in enumerate(new_order):
                    if patient_id in selected_patients:
                        self.selectionModel().select(
                            self.model().index(row, 0),
                            QItemSelectionModel.SelectionFlag.Select
                            | QItemSelectionModel.SelectionFlag.Rows,
                        )
                current_row = (
                    new_order.index(current_patient) if current_patient in new_order else -1
                )
                self.setCurrentCell(
                    current_row, current_column if current_row >= 0 else -1,
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
            # Copy the mutable cell mapping so later caller edits still refresh the row.
            self._rows = tuple(
                PatientStatusRow(row.patient_id, row.variant_count, dict(row.cells))
                for row in rows
            )
            # Keep status text readable on small/high-DPI workstations; let the
            # table scroll horizontally instead of squeezing eight columns.
            metrics = self.fontMetrics()
            patient_width = max((metrics.horizontalAdvance(row.patient_id) + 44 for row in rows), default=136)
            self.setColumnWidth(0, max(136, min(320, patient_width), self.columnWidth(0)))
            for column, database in enumerate([*self.databases, "Report"], start=2):
                minimum_width = 132 if database == "Report" else 110
                width = max((metrics.horizontalAdvance(display_status_label(row.cells[database].label)) + 24 for row in rows), default=minimum_width)
                self.setColumnWidth(column, max(minimum_width, width, self.columnWidth(column)))
            self.verticalScrollBar().setValue(vertical_position)
            self.horizontalScrollBar().setValue(horizontal_position)
        finally:
            self.setUpdatesEnabled(updates_enabled)
            blocker.unblock()
        if previous_checked != self._checked_patients:
            self.patient_selection_changed.emit()
        focused_patient = self.focused_patient()
        if focused_patient != self._focused_patient_id:
            self._notify_patient_focus()
        elif focused_patient is not None and previous_details != self.patient_details():
            self.patient_details_changed.emit(focused_patient)

    def _set_item(self, row: int, column: int, text: str, user_data=None) -> QTableWidgetItem:
        item = self.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.setItem(row, column, item)
        if item.text() != text:
            item.setText(text)
        if item.data(Qt.ItemDataRole.UserRole) != user_data:
            item.setData(Qt.ItemDataRole.UserRole, user_data)
        return item

    def _emit_activation(self, item: QTableWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, tuple) and len(value) == 2:
            self.cell_activated.emit(str(value[0]), str(value[1]))
