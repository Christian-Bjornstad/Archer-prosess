from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QButtonGroup, QFrame, QLabel, QPushButton, QVBoxLayout

from archer_processor.gui.icons import icon


class NavigationRail(QFrame):
    page_requested = pyqtSignal(int)

    def __init__(self, app_icon_path: Path) -> None:
        super().__init__()
        self.setObjectName("NavigationRail")
        self.setFixedWidth(184)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 22, 16, 18)
        layout.setSpacing(8)

        brand_mark = QLabel()
        brand_mark.setObjectName("BrandMark")
        brand_mark.setFixedSize(48, 48)
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_mark.setAccessibleName("VPM Tolkning application icon")
        if app_icon_path.exists():
            brand_mark.setPixmap(
                QPixmap(str(app_icon_path)).scaled(
                    48,
                    48,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        layout.addWidget(brand_mark)
        title = QLabel("VPM Tolkning")
        title.setObjectName("BrandTitle")
        layout.addWidget(title)
        layout.addSpacing(20)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        for index, (label, icon_name) in enumerate(
            (
                ("Import", "document"),
                ("Variants", "table"),
                ("Evidence", "search"),
                ("Settings", "settings"),
            )
        ):
            button = QPushButton(label)
            button.setObjectName("SidebarButton")
            button.setIcon(icon(icon_name))
            button.setCheckable(True)
            button.setMinimumHeight(46)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda checked=False, page=index: self.page_requested.emit(page)
            )
            self.group.addButton(button, index)
            self.buttons.append(button)
            layout.addWidget(button)
        layout.addStretch()
        self.set_current(0)

    def set_current(self, index: int) -> None:
        if 0 <= index < len(self.buttons):
            self.buttons[index].setChecked(True)
