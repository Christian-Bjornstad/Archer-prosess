from __future__ import annotations

from archer_processor.gui.status_model import RunPhase


class Palette:
    ink = "#163445"
    muted = "#607886"
    panel = "#FFFFFF"
    app_bg = "#F3F7F9"
    border = "#D3E0E6"
    navy = "#0B2F43"
    blue = "#087EA4"
    cyan = "#0E98A8"
    green = "#18794E"
    red = "#B42318"
    yellow = "#A15C00"
    pale_blue = "#E7F4F7"
    pale_green = "#E9F6EF"
    strong_green = "#CDEDD8"
    pale_orange = "#FCE4D6"
    artifact_orange = "#FFC000"
    artifact_light_orange = "#F4B183"
    pale_red = "#F8E8E8"
    pale_yellow = "#FFF5D6"


STATE_COLORS = {
    RunPhase.READY: ("#E9F6EF", "#18794E", "#BDD9C3"),
    RunPhase.LOADING: ("#E7F4F7", "#087EA4", "#99CDDA"),
    RunPhase.RUNNING: ("#E7F4F7", "#087EA4", "#99CDDA"),
    RunPhase.PAUSING: ("#FFF5D6", "#714600", "#D7AA4B"),
    RunPhase.PAUSED: ("#FFF5D6", "#714600", "#D7AA4B"),
    RunPhase.STOPPING: ("#F8E8E8", "#B42318", "#E1A6A1"),
    RunPhase.INTERRUPTED: ("#FFF5D6", "#714600", "#D7AA4B"),
    RunPhase.RETRY_AVAILABLE: ("#FFF5D6", "#714600", "#D7AA4B"),
    RunPhase.REPORT_PENDING: ("#FFF5D6", "#714600", "#D7AA4B"),
    RunPhase.COMPLETE: ("#E9F6EF", "#18794E", "#BDD9C3"),
}


def application_stylesheet() -> str:
    """Shared shell styles layered before the existing workspace styles."""
    return f"""
        QFrame#NavigationRail {{
            background: #08283A;
            border: none;
        }}
        QFrame#RunStatusStrip {{
            background: {Palette.panel};
            border: 1px solid {Palette.border};
            border-radius: 8px;
        }}
        QFrame#RecentAnalysisPanel {{
            background: {Palette.pale_blue};
            border: 1px solid #99CDDA;
            border-left: 4px solid {Palette.blue};
            border-radius: 8px;
        }}
        QLabel#RunPhaseLabel {{
            border-radius: 11px;
            padding: 4px 10px;
            font-weight: 700;
        }}
        QLabel#RunProgressLabel {{
            color: {Palette.muted};
            font-weight: 600;
        }}
        QPushButton:focus, QCheckBox:focus, QComboBox:focus,
        QLineEdit:focus, QTableWidget:focus {{
            border: 2px solid #087EA4;
        }}
        QPushButton {{
            min-height: 26px;
            padding: 9px 15px;
        }}
        QPushButton[compact="true"] {{
            min-height: 20px;
            padding: 4px 8px;
        }}
        QPushButton#PrimaryButton, QPushButton#ReportButton {{
            min-height: 26px;
        }}
    """
