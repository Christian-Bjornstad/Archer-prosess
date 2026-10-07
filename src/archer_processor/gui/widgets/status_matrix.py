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


class StatusMatrix(QTableWidget):
    cell_activated = pyqtSignal(str, str)

    def __init__(self, databases: Sequence[str]) -> None:
        self.databases = list(databases)
        super().__init__(0, 3 + len(self.databases))
        self.setObjectName("PatientStatusMatrix")
        self.setHorizontalHeaderLabels(
            ["Patient", "Variants", *self.databases, "Report"]
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
        self.itemActivated.connect(self._emit_activation)
        self._rows: tuple[PatientStatusRow, ...] = ()

    def set_rows(self, rows: Sequence[PatientStatusRow]) -> None:
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
        self.setUpdatesEnabled(False)
        try:
            if self.rowCount() != len(rows):
                self.setRowCount(len(rows))
            for row, row_data in enumerate(rows):
                if row < len(self._rows) and self._rows[row] == row_data:
                    continue
                self._set_item(row, 0, row_data.patient_id, row_data.patient_id)
                self._set_item(row, 1, str(row_data.variant_count))
                for column, database in enumerate([*self.databases, "Report"], start=2):
                    cell = row_data.cells[database]
                    item = self._set_item(
                        row, column, cell.label, (row_data.patient_id, database)
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
            patient_width = max((metrics.horizontalAdvance(row.patient_id) + 24 for row in rows), default=160)
            self.setColumnWidth(0, max(160, min(320, patient_width), self.columnWidth(0)))
            for column, database in enumerate([*self.databases, "Report"], start=2):
                width = max((metrics.horizontalAdvance(row.cells[database].label) + 24 for row in rows), default=128)
                self.setColumnWidth(column, max(128, width, self.columnWidth(column)))
            self.verticalScrollBar().setValue(vertical_position)
            self.horizontalScrollBar().setValue(horizontal_position)
        finally:
            self.setUpdatesEnabled(updates_enabled)
            blocker.unblock()

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
