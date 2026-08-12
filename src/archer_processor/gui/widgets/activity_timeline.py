from __future__ import annotations

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

    def add_activity(self, activity: RunActivity) -> None:
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
            self.setItem(row, column, QTableWidgetItem(value))
