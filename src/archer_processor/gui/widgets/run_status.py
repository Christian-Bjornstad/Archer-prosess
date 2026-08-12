from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from archer_processor.gui.status_model import RunPhase, RunSnapshot
from archer_processor.gui.theme import STATE_COLORS


class RunStatusStrip(QFrame):
    resume_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("RunStatusStrip")
        self.setMinimumHeight(54)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        self.phase_label = QLabel("Ready")
        self.phase_label.setObjectName("RunPhaseLabel")
        self.progress_label = QLabel("No active search")
        self.progress_label.setObjectName("RunProgressLabel")
        self.resume_button = QPushButton("Resume incomplete search")
        self.resume_button.setObjectName("PrimaryButton")
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("PauseButton")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("StopButton")
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        layout.addWidget(self.phase_label)
        layout.addWidget(self.progress_label)
        layout.addStretch()
        layout.addWidget(self.resume_button)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.stop_button)
        self.set_snapshot(RunSnapshot())

    def set_snapshot(self, snapshot: RunSnapshot) -> None:
        background, foreground, border = STATE_COLORS[snapshot.phase]
        self.setProperty("phase", snapshot.phase.value)
        self.phase_label.setText(snapshot.phase.label)
        self.phase_label.setStyleSheet(
            f"background: {background}; color: {foreground}; "
            f"border: 1px solid {border};"
        )
        if snapshot.patient_total:
            self.progress_label.setText(
                f"{snapshot.current_patient} / {snapshot.patient_total} patients"
            )
        else:
            self.progress_label.setText("No active search")
        self.resume_button.setVisible(
            snapshot.phase in {RunPhase.INTERRUPTED, RunPhase.RETRY_AVAILABLE}
        )
        active = snapshot.phase in {
            RunPhase.RUNNING,
            RunPhase.PAUSING,
            RunPhase.PAUSED,
            RunPhase.STOPPING,
        }
        self.pause_button.setVisible(active)
        self.stop_button.setVisible(active)
