from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QDate, QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from archer_processor.core import ProcessingResult, VariantProcessor
from archer_processor.io import ArcherTsvReader
from archer_processor.knowledge import VariantHistoryRepository
from archer_processor.reports import ExcelReportWriter
from archer_processor.services import AppSettings, DatabaseSearchService


class Palette:
    ink = "#17212B"
    muted = "#5E6A73"
    panel = "#FFFFFF"
    app_bg = "#F4F7F9"
    border = "#CBD7E1"
    navy = "#163B5C"
    blue = "#2F75B5"
    green = "#4F8A5B"
    red = "#A92525"
    yellow = "#B98100"
    pale_blue = "#EAF3FA"
    pale_green = "#EAF5ED"
    pale_red = "#F8E8E8"
    pale_yellow = "#FFF5D6"


class ProcessingWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, input_path: Path, output_path: Path, run_date: str, settings: AppSettings, hide_excluded: bool):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.run_date = run_date
        self.settings = settings
        self.hide_excluded = hide_excluded

    def run(self) -> None:
        try:
            self.status.emit("Reading Archer TSV")
            history_path = Path(self.settings.history_workbook)
            history = VariantHistoryRepository(history_path) if history_path.exists() else None
            processor = VariantProcessor(history=history)
            result = processor.process(self.input_path, self.run_date, self.output_path)
            self.status.emit("Writing review workbook")
            ExcelReportWriter().write(result, self.output_path, hide_excluded=self.hide_excluded)
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class DatabaseWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, variants, databases: list[str], settings: AppSettings, max_workers: int):
        super().__init__()
        self.variants = variants
        self.databases = databases
        self.settings = settings
        self.max_workers = max_workers

    def run(self) -> None:
        try:
            service = DatabaseSearchService(self.settings)
            self.status.emit(
                f"Parallel search started: {len(self.variants)} variants, "
                f"{len(self.databases)} sources, {self.max_workers} workers"
            )

            def on_progress(done: int, total: int, variant) -> None:
                self.status.emit(f"Evidence {done}/{total} complete: {variant.display_name}")

            evidence = service.search_variants_parallel(
                self.variants,
                self.databases,
                max_workers=self.max_workers,
                progress=on_progress,
            )
            self.finished.emit(evidence)
        except Exception as exc:
            self.failed.emit(str(exc))


class MetricCard(QFrame):
    def __init__(self, label: str, value: str = "0", accent: str = Palette.blue):
        super().__init__()
        self.setObjectName("MetricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        self.value = QLabel(value)
        self.value.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        self.value.setStyleSheet(f"color: {accent};")
        self.label = QLabel(label)
        self.label.setStyleSheet(f"color: {Palette.muted};")
        layout.addWidget(self.value)
        layout.addWidget(self.label)

    def set_value(self, value: int | str) -> None:
        self.value.setText(str(value))


class MainWindow(QMainWindow):
    databases = ["ClinVar", "MTBP", "HSMD", "COSMIC", "OncoKB", "Franklin", "gnomAD"]

    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings.load()
        self.result: ProcessingResult | None = None
        self.evidence = {}
        self.processing_thread: QThread | None = None
        self.database_thread: QThread | None = None
        self.processing_worker: ProcessingWorker | None = None
        self.database_worker: DatabaseWorker | None = None
        self.setWindowTitle("Archer Prosess")
        self.resize(1280, 820)
        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Archer Prosess")
        title.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {Palette.navy};")
        subtitle = QLabel("VPM variant processing, local history, and database evidence")
        subtitle.setStyleSheet(f"color: {Palette.muted};")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.status_badge = QLabel("Ready")
        self.status_badge.setObjectName("StatusBadge")
        header.addWidget(self.status_badge)
        layout.addLayout(header)

        metrics = QHBoxLayout()
        self.total_card = MetricCard("Total variants", "0", Palette.navy)
        self.included_card = MetricCard("Included", "0", Palette.green)
        self.excluded_card = MetricCard("Excluded", "0", Palette.red)
        self.warning_card = MetricCard("Review flags", "0", Palette.yellow)
        for card in [self.total_card, self.included_card, self.excluded_card, self.warning_card]:
            metrics.addWidget(card)
        layout.addLayout(metrics)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._processing_tab(), "Processing")
        self.tabs.addTab(self._review_tab(), "Review")
        self.tabs.addTab(self._database_tab(), "Databases")
        self.tabs.addTab(self._settings_tab(), "Settings")
        layout.addWidget(self.tabs, 1)

        self.setCentralWidget(root)
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _processing_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)

        files = QGroupBox("Run Setup")
        grid = QGridLayout(files)
        grid.setColumnStretch(1, 1)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Archer all_samples_filtered_variants.tsv")
        self.input_edit.textChanged.connect(self._update_process_state)
        input_btn = QPushButton("Browse")
        input_btn.clicked.connect(self._browse_input)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Output workbook")
        self.output_edit.textChanged.connect(self._update_process_state)
        output_btn = QPushButton("Browse")
        output_btn.clicked.connect(self._browse_output)
        self.run_date = QDateEdit()
        self.run_date.setCalendarPopup(True)
        self.run_date.setDisplayFormat("yyyy-MM-dd")
        self.run_date.setDate(QDate.currentDate())
        self.hide_excluded = QCheckBox("Hide excluded rows in workbook")
        self.hide_excluded.setChecked(True)
        grid.addWidget(QLabel("Input TSV"), 0, 0)
        grid.addWidget(self.input_edit, 0, 1)
        grid.addWidget(input_btn, 0, 2)
        grid.addWidget(QLabel("Output XLSX"), 1, 0)
        grid.addWidget(self.output_edit, 1, 1)
        grid.addWidget(output_btn, 1, 2)
        grid.addWidget(QLabel("Run date"), 2, 0)
        grid.addWidget(self.run_date, 2, 1)
        grid.addWidget(self.hide_excluded, 2, 2)
        layout.addWidget(files)

        actions = QHBoxLayout()
        self.validate_btn = QPushButton("Validate TSV")
        self.validate_btn.clicked.connect(self._validate_input)
        self.process_btn = QPushButton("Process and Export")
        self.process_btn.setObjectName("PrimaryButton")
        self.process_btn.setEnabled(False)
        self.process_btn.clicked.connect(self._start_processing)
        actions.addWidget(self.validate_btn)
        actions.addWidget(self.process_btn)
        actions.addStretch()
        layout.addLayout(actions)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        layout.addWidget(self.log, 1)
        return page

    def _review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.variant_table = QTableWidget(0, 8)
        self.variant_table.setHorizontalHeaderLabels(
            ["Sample", "Gene", "HGVSc", "AF", "Depth", "Decision", "History", "Warnings"]
        )
        self.variant_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.variant_table.setAlternatingRowColors(True)
        self.variant_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.variant_table)
        return page

    def _database_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        checks = QGroupBox("Evidence Sources")
        grid = QGridLayout(checks)
        self.db_checks: dict[str, QCheckBox] = {}
        for index, database in enumerate(self.databases):
            check = QCheckBox(database)
            check.setChecked(database in self.settings.enabled_databases)
            self.db_checks[database] = check
            grid.addWidget(check, index // 4, index % 4)
        layout.addWidget(checks)

        actions = QHBoxLayout()
        actions.addWidget(QLabel("Workers"))
        self.worker_count = QSpinBox()
        self.worker_count.setRange(1, 8)
        self.worker_count.setValue(self.settings.database_workers)
        self.worker_count.setToolTip("Parallel variant searches. Use 2-3 for public APIs without API keys.")
        actions.addWidget(self.worker_count)
        self.search_btn = QPushButton("Search Selected Sources")
        self.search_btn.setEnabled(False)
        self.search_btn.clicked.connect(self._start_database_search)
        self.rewrite_btn = QPushButton("Rewrite Workbook With Evidence")
        self.rewrite_btn.setEnabled(False)
        self.rewrite_btn.clicked.connect(self._rewrite_workbook)
        actions.addWidget(self.search_btn)
        actions.addWidget(self.rewrite_btn)
        actions.addStretch()
        layout.addLayout(actions)

        self.evidence_table = QTableWidget(0, 6)
        self.evidence_table.setHorizontalHeaderLabels(["Sample", "Gene", "HGVSc", "Database", "Status", "Summary"])
        self.evidence_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.evidence_table.setAlternatingRowColors(True)
        layout.addWidget(self.evidence_table, 1)
        return page

    def _settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("Local Configuration")
        grid = QGridLayout(group)
        grid.setColumnStretch(1, 1)
        self.history_edit = QLineEdit(self.settings.history_workbook)
        history_btn = QPushButton("Browse")
        history_btn.clicked.connect(self._browse_history)
        self.output_dir_edit = QLineEdit(self.settings.default_output_dir)
        dir_btn = QPushButton("Browse")
        dir_btn.clicked.connect(self._browse_output_dir)
        self.clinvar_key_edit = QLineEdit(self.settings.clinvar_api_key)
        self.clinvar_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.oncokb_key_edit = QLineEdit(self.settings.oncokb_api_key)
        self.oncokb_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.gnomad_dataset_combo = QComboBox()
        self.gnomad_dataset_combo.addItems(["gnomad_r2_1", "gnomad_r3", "gnomad_r4"])
        self.gnomad_dataset_combo.setCurrentText(self.settings.gnomad_dataset)
        save_btn = QPushButton("Save Settings")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._save_settings)
        grid.addWidget(QLabel("History workbook"), 0, 0)
        grid.addWidget(self.history_edit, 0, 1)
        grid.addWidget(history_btn, 0, 2)
        grid.addWidget(QLabel("Default output folder"), 1, 0)
        grid.addWidget(self.output_dir_edit, 1, 1)
        grid.addWidget(dir_btn, 1, 2)
        grid.addWidget(QLabel("ClinVar API key"), 2, 0)
        grid.addWidget(self.clinvar_key_edit, 2, 1)
        grid.addWidget(QLabel("OncoKB API token"), 3, 0)
        grid.addWidget(self.oncokb_key_edit, 3, 1)
        grid.addWidget(QLabel("gnomAD dataset"), 4, 0)
        grid.addWidget(self.gnomad_dataset_combo, 4, 1)
        grid.addWidget(save_btn, 5, 2)
        layout.addWidget(group)
        layout.addStretch()
        return page

    def _browse_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Archer TSV", "", "TSV files (*.tsv *.txt);;All files (*.*)")
        if path:
            self.input_edit.setText(path)
            output = Path(self.settings.default_output_dir) / f"{Path(path).stem}_archer_review.xlsx"
            self.output_edit.setText(str(output))

    def _browse_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Workbook", "", "Excel workbook (*.xlsx)")
        if path:
            self.output_edit.setText(path if path.lower().endswith(".xlsx") else f"{path}.xlsx")

    def _browse_history(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select History Workbook", "", "Excel workbook (*.xlsx *.xlsm)")
        if path:
            self.history_edit.setText(path)

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Default Output Folder", self.output_dir_edit.text())
        if path:
            self.output_dir_edit.setText(path)

    def _validate_input(self) -> None:
        path = Path(self.input_edit.text())
        ok, errors, warnings = ArcherTsvReader().validate(path)
        self._log("Validation passed" if ok else "Validation failed")
        for message in warnings + errors:
            self._log(message)
        if ok:
            if not self.output_edit.text().strip():
                output = Path(self.settings.default_output_dir) / f"{path.stem}_archer_review.xlsx"
                self.output_edit.setText(str(output))
            self._update_process_state()
            QMessageBox.information(self, "Validation", "TSV validation passed.")
        else:
            QMessageBox.critical(self, "Validation failed", "\n".join(errors))

    def _start_processing(self) -> None:
        input_path = Path(self.input_edit.text())
        output_path = Path(self.output_edit.text())
        if not input_path.exists() or not self.output_edit.text().strip():
            QMessageBox.warning(self, "Missing files", "Select an input TSV and output workbook.")
            return
        if output_path.suffix.lower() != ".xlsx":
            output_path = output_path.with_suffix(".xlsx")
            self.output_edit.setText(str(output_path))
        self._save_settings(silent=True)
        self._set_busy("Processing")
        worker = ProcessingWorker(
            input_path,
            output_path,
            self.run_date.date().toString("yyyy-MM-dd"),
            self.settings,
            self.hide_excluded.isChecked(),
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status.connect(self._log)
        worker.finished.connect(self._processing_finished)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.processing_thread = thread
        self.processing_worker = worker
        thread.start()

    def _processing_finished(self, result: ProcessingResult) -> None:
        self.result = result
        self._log(f"Complete: {result.total_count} variants, {len(result.included)} included, {len(result.excluded)} excluded")
        self._refresh_metrics()
        self._refresh_variant_table()
        self.search_btn.setEnabled(True)
        self.rewrite_btn.setEnabled(True)
        self._set_ready()
        QMessageBox.information(self, "Complete", f"Workbook saved:\n{result.output_path}")

    def _start_database_search(self) -> None:
        if not self.result:
            return
        self._save_settings(silent=True)
        databases = [name for name, check in self.db_checks.items() if check.isChecked()]
        if not databases:
            QMessageBox.warning(self, "No sources", "Select at least one evidence source.")
            return
        self._set_busy("Searching")
        worker = DatabaseWorker(self.result.included, databases, self.settings, self.worker_count.value())
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status.connect(self._log)
        worker.finished.connect(self._database_finished)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.database_thread = thread
        self.database_worker = worker
        thread.start()

    def _database_finished(self, evidence: dict) -> None:
        self.evidence = evidence
        self._refresh_evidence_table()
        self._set_ready()
        self._log("Database evidence search complete")

    def _rewrite_workbook(self) -> None:
        if not self.result or not self.result.output_path:
            return
        ExcelReportWriter().write(self.result, self.result.output_path, self.evidence, self.hide_excluded.isChecked())
        QMessageBox.information(self, "Workbook Updated", f"Evidence written to:\n{self.result.output_path}")

    def _worker_failed(self, message: str) -> None:
        self._set_ready()
        self._log(f"ERROR: {message}")
        QMessageBox.critical(self, "Error", message)

    def _save_settings(self, silent: bool = False) -> None:
        self.settings.history_workbook = self.history_edit.text()
        self.settings.default_output_dir = self.output_dir_edit.text()
        self.settings.clinvar_api_key = self.clinvar_key_edit.text()
        self.settings.oncokb_api_key = self.oncokb_key_edit.text()
        self.settings.database_workers = self.worker_count.value()
        self.settings.gnomad_dataset = self.gnomad_dataset_combo.currentText()
        self.settings.enabled_databases = [name for name, check in self.db_checks.items() if check.isChecked()]
        self.settings.save()
        if not silent:
            self._log("Settings saved")

    def _refresh_metrics(self) -> None:
        if not self.result:
            return
        self.total_card.set_value(self.result.total_count)
        self.included_card.set_value(len(self.result.included))
        self.excluded_card.set_value(len(self.result.excluded))
        self.warning_card.set_value(sum(1 for variant in self.result.variants if variant.warnings))

    def _refresh_variant_table(self) -> None:
        if not self.result:
            return
        self.variant_table.setRowCount(0)
        for variant in self.result.variants:
            row = self.variant_table.rowCount()
            self.variant_table.insertRow(row)
            values = [
                variant.sample,
                variant.symbol,
                variant.hgvsc,
                "" if variant.af is None else f"{variant.af:.2%}",
                "" if variant.depth is None else str(variant.depth),
                variant.decision,
                str(len(variant.history_matches)),
                "; ".join(variant.warnings),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if variant.decision == "excluded":
                    item.setBackground(QColor(Palette.pale_red))
                elif variant.warnings:
                    item.setBackground(QColor(Palette.pale_yellow))
                elif variant.history_matches:
                    item.setBackground(QColor(Palette.pale_blue))
                self.variant_table.setItem(row, col, item)

    def _refresh_evidence_table(self) -> None:
        self.evidence_table.setRowCount(0)
        if not self.result:
            return
        by_key = {f"{variant.sample}|{variant.hgvsc}": variant for variant in self.result.variants}
        for key, evidence_items in self.evidence.items():
            variant = by_key.get(key)
            if not variant:
                continue
            for evidence in evidence_items:
                row = self.evidence_table.rowCount()
                self.evidence_table.insertRow(row)
                values = [variant.sample, variant.symbol, variant.hgvsc, evidence.database, evidence.status, evidence.summary]
                for col, value in enumerate(values):
                    self.evidence_table.setItem(row, col, QTableWidgetItem(value))

    def _set_busy(self, label: str) -> None:
        self.status_badge.setText(label)
        self.process_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.status_bar.showMessage(label)

    def _set_ready(self) -> None:
        self.status_badge.setText("Ready")
        self._update_process_state()
        self.search_btn.setEnabled(self.result is not None)
        self.status_bar.showMessage("Ready", 3000)

    def _update_process_state(self) -> None:
        has_input = Path(self.input_edit.text()).exists()
        has_output = bool(self.output_edit.text().strip())
        self.process_btn.setEnabled(has_input and has_output)

    def _log(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.status_bar.showMessage(message, 5000)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{
                background: {Palette.app_bg};
                color: {Palette.ink};
                font-family: "Segoe UI";
                font-size: 13px;
            }}
            QGroupBox {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 6px;
                margin-top: 14px;
                padding: 16px 12px 12px 12px;
                font-weight: 600;
                color: {Palette.navy};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }}
            QLineEdit, QDateEdit, QPlainTextEdit, QTableWidget {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 5px;
                padding: 6px;
            }}
            QPushButton {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 5px;
                padding: 8px 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border-color: {Palette.blue};
            }}
            QPushButton:disabled {{
                color: #9AA5AD;
                background: #EEF2F5;
            }}
            QPushButton#PrimaryButton {{
                background: {Palette.blue};
                color: white;
                border-color: {Palette.blue};
            }}
            QFrame#MetricCard {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 6px;
            }}
            QLabel#StatusBadge {{
                background: {Palette.pale_green};
                color: {Palette.green};
                border: 1px solid #BDD9C3;
                border-radius: 12px;
                padding: 5px 12px;
                font-weight: 700;
            }}
            QTabWidget::pane {{
                border: 1px solid {Palette.border};
                background: {Palette.panel};
                border-radius: 6px;
            }}
            QTabBar::tab {{
                padding: 9px 18px;
                background: #E8EEF3;
                border: 1px solid {Palette.border};
                border-bottom: none;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background: {Palette.panel};
                color: {Palette.navy};
                font-weight: 700;
            }}
            QHeaderView::section {{
                background: {Palette.navy};
                color: white;
                padding: 8px;
                border: none;
                font-weight: 700;
            }}
            """
        )


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
