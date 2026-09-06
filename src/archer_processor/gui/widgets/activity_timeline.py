from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel, QTableWidget, QTableWidgetItem

from archer_processor.gui.status_model import RunActivity


class CurrentActivityPanel(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("CurrentActivityPanel")
        layout = QGridLayout(self)
        self.patient_value = QLabel("—")
        self.provider_value = QLabel("—")
        self.variant_value = QLabel("—")
        self.action_value = QLabel("No search is running")
        for column, (label, value) in enumerate(
            (
                ("Patient", self.patient_value),
                ("Provider", self.provider_value),
                ("Variant", self.variant_value),
                ("Current action", self.action_value),
            )
        ):
            heading = QLabel(label)
            heading.setObjectName("SettingsColumnHeader")
            value.setObjectName("FieldLabel")
            value.setWordWrap(True)
            layout.addWidget(heading, 0, column)
            layout.addWidget(value, 1, column)

    def set_activity(self, activity: RunActivity) -> None:
        self.patient_value.setText(activity.patient_id or "—")
        self.provider_value.setText(activity.database or "—")
        self.variant_value.setText(activity.variant_label or "—")
        self.action_value.setText(activity.action or activity.message or "Working")


class ActivityTimeline(QTableWidget):
    MAX_EVENTS = 2_000

    def __init__(self) -> None:
        super().__init__(0, 5)
        self.setObjectName("ActivityTimeline")
        self.setHorizontalHeaderLabels(
            ["Time", "Patient", "Provider", "Action", "Message"]
        )
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.setAlternatingRowColors(True)
        self.setWordWrap(True)
        for column, width in enumerate((76, 115, 90, 140, 400)):
            self.setColumnWidth(column, width)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)

    def add_activity(self, activity: RunActivity) -> None:
        follow = self.verticalScrollBar().value() >= self.verticalScrollBar().maximum() - 2
        if self.rowCount() >= self.MAX_EVENTS:
            self.removeRow(0)
        row = self.rowCount()
        self.insertRow(row)
        values = [
            activity.occurred_at.strftime("%H:%M:%S"),
            activity.patient_id,
            activity.database,
            activity.action,
            activity.message,
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self.setItem(row, column, item)
        self.resizeRowToContents(row)
        if follow:
            self.scrollToBottom()
