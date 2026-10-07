from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(8)

        self.phase_label = QLabel("Klar")
        self.phase_label.setObjectName("RunPhaseLabel")
        self.progress_label = QLabel("Ingen aktiv kjøring")
        self.progress_label.setObjectName("RunProgressLabel")
        self.progress_label.setWordWrap(True)
        self.resume_button = QPushButton("Fortsett uferdige oppslag")
        self.resume_button.setObjectName("PrimaryButton")
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("PauseButton")
        self.stop_button = QPushButton("Stopp")
        self.stop_button.setObjectName("StopButton")
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        row.addWidget(self.phase_label)
        row.addWidget(self.progress_label, 1)
        row.addWidget(self.resume_button)
        row.addWidget(self.pause_button)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        self.set_snapshot(RunSnapshot())

    def set_snapshot(self, snapshot: RunSnapshot) -> None:
        background, foreground, border = STATE_COLORS[snapshot.phase]
        self.setProperty("phase", snapshot.phase.value)
        self.phase_label.setText({
            RunPhase.READY: "Klar", RunPhase.LOADING: "Arbeider",
            RunPhase.RUNNING: "Søker", RunPhase.PAUSING: "Setter på pause",
            RunPhase.PAUSED: "På pause", RunPhase.STOPPING: "Stopper",
            RunPhase.INTERRUPTED: "Stoppet", RunPhase.RETRY_AVAILABLE: "Oppslag gjenstår",
            RunPhase.COMPLETE: "Utført", RunPhase.REPORT_PENDING: "Lagring venter",
        }[snapshot.phase])
        self.phase_label.setStyleSheet(
            f"background: {background}; color: {foreground}; "
            f"border: 1px solid {border};"
        )
        if snapshot.patient_total:
            self.progress_label.setText(
                f"{snapshot.current_patient} / {snapshot.patient_total} pasienter"
            )
        else:
            self.progress_label.setText({
                RunPhase.READY: "Ingen aktiv kjøring",
                RunPhase.LOADING: "Arbeider med analysen",
                RunPhase.RUNNING: "Fortsetter søket",
                RunPhase.PAUSING: "Venter på et trygt stoppunkt",
                RunPhase.PAUSED: "Fortsett fra samme kø",
                RunPhase.STOPPING: "Beholder utførte oppslag",
                RunPhase.INTERRUPTED: "Fortsett ventende oppslag med søk-knappen",
                RunPhase.RETRY_AVAILABLE: "Noen oppslag må prøves igjen",
                RunPhase.COMPLETE: "Oppslag utført",
                RunPhase.REPORT_PENDING: "Oppslag er beholdt · lagring gjenstår",
            }[snapshot.phase])
        if snapshot.action:
            action = {
                "Processing": "Oppretter review-fil",
                "Loading workbook": "Gjenåpner analyse",
                "Checking browser sessions": "Sjekker innlogging",
            }.get(snapshot.action, snapshot.action)
            action = action.replace("variant(s)", "varianter").replace("source(s)", "kilder")
            action = action.replace("variants", "varianter").replace("sources", "kilder")
            self.progress_label.setText(self.progress_label.text() + " · " + action)
        # Search recovery uses the single main action on the Evidence page.
        self.resume_button.hide()
        active = snapshot.phase in {
            RunPhase.RUNNING,
            RunPhase.PAUSING,
            RunPhase.PAUSED,
            RunPhase.STOPPING,
        }
        self.pause_button.setVisible(active)
        self.stop_button.setVisible(active)
        self.pause_button.setEnabled(active and snapshot.phase not in {RunPhase.PAUSING, RunPhase.STOPPING})
        self.stop_button.setEnabled(active and snapshot.phase != RunPhase.STOPPING)
