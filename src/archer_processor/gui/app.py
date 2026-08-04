from __future__ import annotations

import sys
import random
import time
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
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from archer_processor.core import DatabaseEvidence, FilterEngine, ProcessingResult, VariantProcessor, default_artifact_rules, production_rules
from archer_processor.core.highlights import variant_highlight
from archer_processor.io import ArcherTsvReader
from archer_processor.knowledge import VariantHistoryRepository
from archer_processor.reports import (
    ExcelReportWriter,
    PatientExcelReportWriter,
    PatientPdfReportWriter,
)
from archer_processor.services import (
    AppSettings,
    BROWSER_DATABASES,
    BrowserReviewService,
    DatabaseSearchService,
    load_database_skip_keys,
)


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
    pale_orange = "#FCE4D6"
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
            filter_engine = FilterEngine(production_rules(self.settings.artifact_rules))
            processor = VariantProcessor(history=history, filter_engine=filter_engine)
            result = processor.process(self.input_path, self.run_date, self.output_path)
            self.status.emit("Writing review workbook")
            ExcelReportWriter().write(result, self.output_path, hide_excluded=self.hide_excluded)
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


def _variants_grouped_by_patient(variants) -> list[tuple[str, list]]:
    grouped: dict[str, list] = {}
    for variant in variants:
        grouped.setdefault(variant.patient_id, []).append(variant)
    return list(grouped.items())


def _merge_evidence_results(target: dict, incoming: dict) -> None:
    for key, new_items in incoming.items():
        by_database = {item.database: item for item in target.get(key, [])}
        by_database.update({item.database: item for item in new_items})
        target[key] = list(by_database.values())


class DatabaseWorker(QObject):
    finished = pyqtSignal(object)
    patient_finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(
        self,
        variants,
        api_databases: list[str],
        browser_databases: list[str],
        artifact_root: Path,
        settings: AppSettings,
    ):
        super().__init__()
        self.variants = variants
        self.api_databases = api_databases
        self.browser_databases = browser_databases
        self.databases = [*api_databases, *browser_databases]
        self.artifact_root = artifact_root
        self.settings = settings

    def run(self) -> None:
        try:
            api_service = DatabaseSearchService(self.settings)
            browser_service = self._browser_service()
            for database, status in api_service.database_diagnostics(self.databases).items():
                self.status.emit(f"{database}: {status}")
            patients = _variants_grouped_by_patient(self.variants)
            all_evidence: dict[str, list[DatabaseEvidence]] = {}
            self.status.emit(
                f"Patient-by-patient search started: {len(patients)} patients, "
                f"{len(self.databases)} sources"
            )
            for patient_index, (patient_id, patient_variants) in enumerate(patients, start=1):
                patient_evidence: dict[str, list[DatabaseEvidence]] = {
                    api_service.variant_key(variant): [] for variant in patient_variants
                }
                prefix = f"Patient {patient_index}/{len(patients)} ({patient_id})"
                self.status.emit(f"{prefix}: starting {len(patient_variants)} variant(s)")
                for database in self.api_databases:
                    self.status.emit(f"{prefix}: searching {database}")
                    for variant in patient_variants:
                        key = api_service.variant_key(variant)
                        try:
                            patient_evidence[key].extend(
                                api_service.search_variant(variant, [database])
                            )
                        except Exception as exc:
                            patient_evidence[key].append(
                                DatabaseEvidence(database, "error", str(exc))
                            )

                if self.browser_databases:
                    if patient_index > 1:
                        self._wait(
                            f"{prefix}: website safety buffer before signed-in sources"
                        )
                    browser_evidence = browser_service.search_variants(
                        patient_variants,
                        self.browser_databases,
                        self.artifact_root / f"patient-{patient_index:03d}",
                        progress=lambda message, p=prefix: self.status.emit(f"{p}: {message}"),
                    )
                    _merge_evidence_results(patient_evidence, browser_evidence)

                _merge_evidence_results(all_evidence, patient_evidence)
                self.patient_finished.emit(patient_evidence)
                self.status.emit(f"{prefix}: complete")
            self.finished.emit(all_evidence)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _browser_service(self) -> BrowserReviewService:
        return BrowserReviewService(
            mtbp_cancer_type=self.settings.mtbp_cancer_type,
            clinvar_api_key=self.settings.clinvar_api_key,
            analysis_timeout_ms=self.settings.mtbp_timeout_minutes * 60_000,
            request_delay_ms=self.settings.browser_delay_seconds * 1_000,
            request_delay_max_ms=self.settings.browser_delay_max_seconds * 1_000,
            cosmic_email=self.settings.cosmic_email,
            cosmic_password=self.settings.cosmic_password,
            oncokb_email=self.settings.oncokb_email,
            oncokb_password=self.settings.oncokb_password,
            franklin_email=self.settings.franklin_email,
            franklin_password=self.settings.franklin_password,
            mtbp_email=self.settings.mtbp_email,
            mtbp_password=self.settings.mtbp_password,
        )

    def _wait(self, reason: str) -> None:
        minimum = max(0, int(self.settings.browser_delay_seconds))
        maximum = max(minimum, int(self.settings.browser_delay_max_seconds))
        delay = random.randint(minimum, maximum)
        if delay <= 0:
            return
        self.status.emit(f"{reason}: {delay}s")
        time.sleep(delay)


class BrowserLoginWorker(QObject):
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, database: str, settings: AppSettings):
        super().__init__()
        self.database = database
        self.settings = settings

    def run(self) -> None:
        try:
            self.status.emit(
                f"{self.database}: checking saved credentials/session"
            )
            service = BrowserReviewService(
                mtbp_cancer_type=self.settings.mtbp_cancer_type,
                clinvar_api_key=self.settings.clinvar_api_key,
                analysis_timeout_ms=self.settings.mtbp_timeout_minutes * 60_000,
                request_delay_ms=self.settings.browser_delay_seconds * 1_000,
                request_delay_max_ms=self.settings.browser_delay_max_seconds * 1_000,
                cosmic_email=self.settings.cosmic_email,
                cosmic_password=self.settings.cosmic_password,
                oncokb_email=self.settings.oncokb_email,
                oncokb_password=self.settings.oncokb_password,
                franklin_email=self.settings.franklin_email,
                franklin_password=self.settings.franklin_password,
                mtbp_email=self.settings.mtbp_email,
                mtbp_password=self.settings.mtbp_password,
            )
            self.finished.emit(service.open_login(self.database))
        except Exception as exc:
            self.failed.emit(str(exc))


class BrowserReviewWorker(QObject):
    finished = pyqtSignal(object)
    patient_finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, variants, databases: list[str], artifact_root: Path, settings: AppSettings):
        super().__init__()
        self.variants = variants
        self.databases = databases
        self.artifact_root = artifact_root
        self.settings = settings

    def run(self) -> None:
        try:
            service = BrowserReviewService(
                mtbp_cancer_type=self.settings.mtbp_cancer_type,
                clinvar_api_key=self.settings.clinvar_api_key,
                analysis_timeout_ms=self.settings.mtbp_timeout_minutes * 60_000,
                request_delay_ms=self.settings.browser_delay_seconds * 1_000,
                request_delay_max_ms=self.settings.browser_delay_max_seconds * 1_000,
                cosmic_email=self.settings.cosmic_email,
                cosmic_password=self.settings.cosmic_password,
                oncokb_email=self.settings.oncokb_email,
                oncokb_password=self.settings.oncokb_password,
                franklin_email=self.settings.franklin_email,
                franklin_password=self.settings.franklin_password,
                mtbp_email=self.settings.mtbp_email,
                mtbp_password=self.settings.mtbp_password,
            )
            all_evidence: dict[str, list[DatabaseEvidence]] = {}
            patients = _variants_grouped_by_patient(self.variants)
            for patient_index, (patient_id, patient_variants) in enumerate(patients, start=1):
                prefix = f"Patient {patient_index}/{len(patients)} ({patient_id})"
                patient_evidence = service.search_variants(
                    patient_variants,
                    self.databases,
                    self.artifact_root / f"patient-{patient_index:03d}",
                    progress=lambda message, p=prefix: self.status.emit(f"{p}: {message}"),
                )
                _merge_evidence_results(all_evidence, patient_evidence)
                self.patient_finished.emit(patient_evidence)
                self.status.emit(f"{prefix}: browser sources complete")
                if patient_index < len(patients):
                    delay = random.randint(
                        max(0, int(self.settings.browser_delay_seconds)),
                        max(
                            int(self.settings.browser_delay_seconds),
                            int(self.settings.browser_delay_max_seconds),
                        ),
                    )
                    if delay > 0:
                        self.status.emit(
                            f"{prefix}: safety buffer before next patient: {delay}s"
                        )
                        time.sleep(delay)
            self.finished.emit(all_evidence)
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


class WorkflowStep(QFrame):
    def __init__(self, number: str, title: str, description: str):
        super().__init__()
        self.setObjectName("WorkflowStep")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        badge = QLabel(number)
        badge.setObjectName("StepNumber")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(30, 30)
        copy = QVBoxLayout()
        copy.setSpacing(1)
        title_label = QLabel(title)
        title_label.setObjectName("StepTitle")
        description_label = QLabel(description)
        description_label.setObjectName("StepDescription")
        description_label.setWordWrap(True)
        copy.addWidget(title_label)
        copy.addWidget(description_label)
        layout.addWidget(badge)
        layout.addLayout(copy, 1)


class MainWindow(QMainWindow):
    databases = [
        "MTBP",
        "Franklin",
        "ClinVar",
        "OncoKB",
        "COSMIC",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings.load()
        self.result: ProcessingResult | None = None
        self.evidence = {}
        self.database_skip_keys: set[str] = set()
        self.processing_thread: QThread | None = None
        self.database_thread: QThread | None = None
        self.browser_thread: QThread | None = None
        self.processing_worker: ProcessingWorker | None = None
        self.database_worker: DatabaseWorker | None = None
        self.browser_worker: BrowserLoginWorker | BrowserReviewWorker | None = None
        self.setWindowTitle("Archer Prosess")
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)
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
        self.activity_progress = QProgressBar()
        self.activity_progress.setObjectName("ActivityProgress")
        self.activity_progress.setRange(0, 0)
        self.activity_progress.setTextVisible(False)
        self.activity_progress.setFixedWidth(150)
        self.activity_progress.hide()
        header.addWidget(self.activity_progress)
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

        workflow = QHBoxLayout()
        workflow.setSpacing(10)
        workflow.addWidget(WorkflowStep("1", "Prepare", "Validate and process the Archer TSV"))
        workflow.addWidget(WorkflowStep("2", "Collect", "Gather API and signed-in evidence"))
        workflow.addWidget(WorkflowStep("3", "Report", "Export workbook and patient PDFs"))
        layout.addLayout(workflow)

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
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.database_scroll = QScrollArea()
        self.database_scroll.setWidgetResizable(True)
        self.database_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.database_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        database_content = QWidget()
        layout = QVBoxLayout(database_content)
        layout.setSpacing(12)
        intro = QLabel(
            "Choose only the evidence sources needed for this review. The app completes "
            "one patient at a time; website sources use a randomized safety delay and MTBP runs last."
        )
        intro.setObjectName("PageIntro")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        checks = QGroupBox("1. Choose Evidence Sources")
        checks.setMinimumHeight(185)
        grid = QGridLayout(checks)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(8)
        self.db_checks: dict[str, QCheckBox] = {}
        for index, database in enumerate(self.databases):
            check = QCheckBox(database)
            check.setChecked(database in self.settings.enabled_databases)
            if database in BROWSER_DATABASES:
                check.setProperty("loginSource", True)
                check.setToolTip("Uses the saved signed-in browser session.")
            self.db_checks[database] = check
            grid.addWidget(check, index // 4, index % 4)
        scope_row = (len(self.databases) - 1) // 4 + 1
        scope_label = QLabel("Lookup scope")
        scope_label.setObjectName("FieldLabel")
        grid.addWidget(scope_label, scope_row, 0)
        self.included_only_check = QCheckBox("Included variants only")
        self.included_only_check.setChecked(self.settings.search_included_only)
        self.included_only_check.setToolTip(
            "When enabled, excluded and flagged variants are not sent to any database website."
        )
        grid.addWidget(self.included_only_check, scope_row, 1)
        grid.addWidget(QLabel("API workers / patient"), scope_row, 2)
        self.worker_count = QSpinBox()
        self.worker_count.setRange(1, 1)
        self.worker_count.setValue(1)
        self.worker_count.setToolTip(
            "Patient-centric evidence collection is deliberately serial to avoid bursts of requests."
        )
        grid.addWidget(self.worker_count, scope_row, 3)
        selection_row = scope_row + 1
        selection_label = QLabel("Reviewed workbook")
        selection_label.setObjectName("FieldLabel")
        grid.addWidget(selection_label, selection_row, 0)
        self.selection_status = QLabel("No skip list loaded")
        self.selection_status.setObjectName("HelperText")
        self.selection_status.setWordWrap(True)
        grid.addWidget(self.selection_status, selection_row, 1, 1, 2)
        self.load_selection_btn = QPushButton("Load X Selections")
        self.load_selection_btn.setEnabled(False)
        self.load_selection_btn.setToolTip(
            "Load the processed workbook and skip rows marked X on Database Selection."
        )
        self.load_selection_btn.clicked.connect(self._load_database_selection)
        grid.addWidget(self.load_selection_btn, selection_row, 3)
        layout.addWidget(checks)

        collection = QGroupBox("2. Collect Evidence")
        collection.setMinimumHeight(175)
        collection_grid = QGridLayout(collection)
        collection_grid.setColumnStretch(1, 1)
        collection_grid.setHorizontalSpacing(12)
        collection_grid.setVerticalSpacing(9)
        automated_label = QLabel("Selected sources")
        automated_label.setObjectName("FieldLabel")
        automated_help = QLabel(
            "Completes every selected source for one patient before starting the next; MTBP runs last."
        )
        automated_help.setObjectName("HelperText")
        automated_help.setWordWrap(True)
        self.search_btn = QPushButton("Search Selected Sources")
        self.search_btn.setObjectName("PrimaryButton")
        self.search_btn.setEnabled(False)
        self.search_btn.clicked.connect(self._start_database_search)
        collection_grid.addWidget(automated_label, 0, 0)
        collection_grid.addWidget(automated_help, 0, 1)
        collection_grid.addWidget(self.search_btn, 0, 2)

        browser_label = QLabel("Signed-in session")
        browser_label.setObjectName("FieldLabel")
        self.browser_database_combo = QComboBox()
        self.browser_database_combo.addItems(list(BROWSER_DATABASES))
        self.browser_signin_btn = QPushButton("Sign In / Refresh")
        self.browser_signin_btn.setToolTip(
            "Uses credentials stored by Windows Credential Manager, or opens Edge for manual sign-in. The profile is released automatically after success."
        )
        self.browser_signin_btn.clicked.connect(self._start_browser_login)
        self.browser_review_btn = QPushButton("Run Browser Lookups")
        self.browser_review_btn.setObjectName("OutlineButton")
        self.browser_review_btn.setEnabled(False)
        self.browser_review_btn.setToolTip(
            "Runs selected ClinVar, COSMIC, OncoKB, Franklin, and MTBP sources patient-by-patient in visible Edge."
        )
        self.browser_review_btn.clicked.connect(self._start_browser_review)
        session_actions = QHBoxLayout()
        session_actions.setContentsMargins(0, 0, 0, 0)
        session_actions.addWidget(self.browser_database_combo)
        session_actions.addWidget(self.browser_signin_btn)
        session_actions.addWidget(self.browser_review_btn)
        collection_grid.addWidget(browser_label, 1, 0)
        collection_grid.addLayout(session_actions, 1, 1, 1, 2)
        self.browser_security_label = QLabel(
            "Credentials stay in Windows Credential Manager. Patient/sample IDs are never submitted; only variant coordinates are sent."
        )
        self.browser_security_label.setObjectName("SecurityNote")
        self.browser_security_label.setWordWrap(True)
        collection_grid.addWidget(self.browser_security_label, 2, 1, 1, 2)
        layout.addWidget(collection)

        exports = QGroupBox("3. Create Reviewed Outputs")
        exports.setMinimumHeight(105)
        export_layout = QHBoxLayout(exports)
        export_help = QLabel(
            "Patient Excel reports use Oversikt, Vedlegg, and one sheet per variant with MTBP, Franklin, ClinVar, OncoKB, and COSMIC captures."
        )
        export_help.setObjectName("HelperText")
        export_help.setWordWrap(True)
        self.rewrite_btn = QPushButton("Rewrite Workbook With Evidence")
        self.rewrite_btn.setEnabled(False)
        self.rewrite_btn.clicked.connect(self._rewrite_workbook)
        self.patient_excel_btn = QPushButton("Export Patient Excel Reports")
        self.patient_excel_btn.setObjectName("ReportButton")
        self.patient_excel_btn.setEnabled(False)
        self.patient_excel_btn.setToolTip(
            "Creates one image-led Excel evidence report per DIT/patient."
        )
        self.patient_excel_btn.clicked.connect(self._export_patient_excels)
        self.patient_pdf_btn = QPushButton("Export Patient PDFs")
        self.patient_pdf_btn.setEnabled(False)
        self.patient_pdf_btn.setToolTip(
            "Creates one clinical review PDF per DIT containing included variants and captured evidence."
        )
        self.patient_pdf_btn.clicked.connect(self._export_patient_pdfs)
        export_layout.addWidget(export_help, 1)
        export_layout.addWidget(self.rewrite_btn)
        export_layout.addWidget(self.patient_excel_btn)
        export_layout.addWidget(self.patient_pdf_btn)
        layout.addWidget(exports)

        evidence_group = QGroupBox("Evidence Results")
        evidence_group.setMinimumHeight(330)
        evidence_layout = QVBoxLayout(evidence_group)
        self.evidence_table = QTableWidget(0, 3 + len(self.databases))
        self.evidence_table.setHorizontalHeaderLabels(["Sample", "Gene", "HGVSc", *self.databases])
        self.evidence_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.evidence_table.setColumnWidth(0, 175)
        self.evidence_table.setColumnWidth(1, 90)
        self.evidence_table.setColumnWidth(2, 245)
        for column in range(3, self.evidence_table.columnCount()):
            self.evidence_table.setColumnWidth(column, 210)
        self.evidence_table.verticalHeader().setDefaultSectionSize(70)
        self.evidence_table.setAlternatingRowColors(True)
        self.evidence_table.setWordWrap(True)
        self.evidence_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        evidence_layout.addWidget(self.evidence_table)
        layout.addWidget(evidence_group)
        self.database_scroll.setWidget(database_content)
        page_layout.addWidget(self.database_scroll)
        return page

    def _settings_tab(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_content = QWidget()
        layout = QVBoxLayout(settings_content)
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
        self.cosmic_email_edit = QLineEdit(self.settings.cosmic_email)
        self.cosmic_password_edit = QLineEdit()
        self.cosmic_password_edit.setText(self.settings.cosmic_password)
        self.cosmic_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.cosmic_password_edit.setPlaceholderText("Stored in Windows Credential Manager")
        self.oncokb_key_edit = QLineEdit(self.settings.oncokb_api_key)
        self.oncokb_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.oncokb_email_edit = QLineEdit(self.settings.oncokb_email)
        self.oncokb_password_edit = QLineEdit()
        self.oncokb_password_edit.setText(self.settings.oncokb_password)
        self.oncokb_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.oncokb_password_edit.setPlaceholderText("Stored in Windows Credential Manager")
        self.franklin_key_edit = QLineEdit(self.settings.franklin_api_key)
        self.franklin_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.franklin_email_edit = QLineEdit(self.settings.franklin_email)
        self.franklin_password_edit = QLineEdit()
        self.franklin_password_edit.setText(self.settings.franklin_password)
        self.franklin_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.franklin_password_edit.setPlaceholderText("Stored in Windows Credential Manager")
        self.mtbp_email_edit = QLineEdit(self.settings.mtbp_email)
        self.mtbp_password_edit = QLineEdit()
        self.mtbp_password_edit.setText(self.settings.mtbp_password)
        self.mtbp_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.mtbp_password_edit.setPlaceholderText("Stored in Windows Credential Manager")
        self.mtbp_cancer_type_edit = QLineEdit(self.settings.mtbp_cancer_type)
        self.mtbp_cancer_type_edit.setPlaceholderText("Exact MTBP cancer type, for example Blood")
        self.browser_delay_spin = QSpinBox()
        self.browser_delay_spin.setRange(0, 120)
        self.browser_delay_spin.setSuffix(" s")
        self.browser_delay_spin.setValue(self.settings.browser_delay_seconds)
        self.browser_delay_spin.setToolTip(
            "Minimum randomized pause between signed-in website searches and providers. APIs are not delayed."
        )
        self.browser_delay_max_spin = QSpinBox()
        self.browser_delay_max_spin.setRange(0, 120)
        self.browser_delay_max_spin.setSuffix(" s")
        self.browser_delay_max_spin.setValue(
            self.settings.browser_delay_max_seconds
        )
        self.browser_delay_max_spin.setToolTip(
            "Maximum randomized pause between signed-in website searches and providers. APIs are not delayed."
        )
        delay_layout = QHBoxLayout()
        delay_layout.setContentsMargins(0, 0, 0, 0)
        delay_layout.addWidget(QLabel("Minimum"))
        delay_layout.addWidget(self.browser_delay_spin)
        delay_layout.addSpacing(12)
        delay_layout.addWidget(QLabel("Maximum"))
        delay_layout.addWidget(self.browser_delay_max_spin)
        delay_layout.addStretch()
        self.mtbp_timeout_spin = QSpinBox()
        self.mtbp_timeout_spin.setRange(5, 60)
        self.mtbp_timeout_spin.setSuffix(" min")
        self.mtbp_timeout_spin.setValue(self.settings.mtbp_timeout_minutes)
        self.artifact_table = QTableWidget(0, 3)
        self.artifact_table.setHorizontalHeaderLabels(["Gene", "HGVSc", "Reason"])
        self.artifact_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._load_artifact_table(self.settings.artifact_rules)
        artifact_actions = QHBoxLayout()
        add_artifact_btn = QPushButton("Add Artifact")
        add_artifact_btn.clicked.connect(self._add_artifact_row)
        remove_artifact_btn = QPushButton("Remove Selected")
        remove_artifact_btn.clicked.connect(self._remove_selected_artifact)
        reset_artifact_btn = QPushButton("Reset Defaults")
        reset_artifact_btn.clicked.connect(self._reset_default_artifacts)
        artifact_actions.addWidget(add_artifact_btn)
        artifact_actions.addWidget(remove_artifact_btn)
        artifact_actions.addWidget(reset_artifact_btn)
        artifact_actions.addStretch()
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
        grid.addWidget(QLabel("COSMIC email"), 3, 0)
        grid.addWidget(self.cosmic_email_edit, 3, 1)
        grid.addWidget(QLabel("COSMIC password"), 4, 0)
        grid.addWidget(self.cosmic_password_edit, 4, 1)
        grid.addWidget(QLabel("OncoKB API token"), 5, 0)
        grid.addWidget(self.oncokb_key_edit, 5, 1)
        grid.addWidget(QLabel("OncoKB email"), 6, 0)
        grid.addWidget(self.oncokb_email_edit, 6, 1)
        grid.addWidget(QLabel("OncoKB password"), 7, 0)
        grid.addWidget(self.oncokb_password_edit, 7, 1)
        grid.addWidget(QLabel("Franklin API token"), 8, 0)
        grid.addWidget(self.franklin_key_edit, 8, 1)
        grid.addWidget(QLabel("Franklin email"), 9, 0)
        grid.addWidget(self.franklin_email_edit, 9, 1)
        grid.addWidget(QLabel("Franklin password"), 10, 0)
        grid.addWidget(self.franklin_password_edit, 10, 1)
        grid.addWidget(QLabel("MTBP email"), 11, 0)
        grid.addWidget(self.mtbp_email_edit, 11, 1)
        grid.addWidget(QLabel("MTBP password"), 12, 0)
        grid.addWidget(self.mtbp_password_edit, 12, 1)
        grid.addWidget(QLabel("MTBP cancer type"), 13, 0)
        grid.addWidget(self.mtbp_cancer_type_edit, 13, 1)
        grid.addWidget(QLabel("Website safety delay"), 14, 0)
        grid.addLayout(delay_layout, 14, 1, 1, 2)
        grid.addWidget(QLabel("MTBP report timeout"), 15, 0)
        grid.addWidget(self.mtbp_timeout_spin, 15, 1)
        grid.addWidget(QLabel("Artifact list"), 16, 0)
        grid.addWidget(self.artifact_table, 16, 1, 1, 2)
        grid.addLayout(artifact_actions, 17, 1, 1, 2)
        grid.addWidget(save_btn, 18, 2)
        layout.addWidget(group)
        layout.addStretch()
        self.settings_scroll.setWidget(settings_content)
        page_layout.addWidget(self.settings_scroll)
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

    def _load_artifact_table(self, artifacts: list[dict[str, str]]) -> None:
        self.artifact_table.setRowCount(0)
        for artifact in artifacts:
            row = self.artifact_table.rowCount()
            self.artifact_table.insertRow(row)
            for col, key in enumerate(["gene", "hgvsc", "reason"]):
                self.artifact_table.setItem(row, col, QTableWidgetItem(str(artifact.get(key) or "")))

    def _artifact_rules_from_table(self) -> list[dict[str, str]]:
        artifacts = []
        for row in range(self.artifact_table.rowCount()):
            gene = self._table_text(self.artifact_table, row, 0).upper()
            hgvsc = self._table_text(self.artifact_table, row, 1)
            reason = self._table_text(self.artifact_table, row, 2)
            if hgvsc:
                artifacts.append({"gene": gene, "hgvsc": hgvsc, "reason": reason})
        return artifacts

    def _add_artifact_row(self) -> None:
        row = self.artifact_table.rowCount()
        self.artifact_table.insertRow(row)
        for col in range(3):
            self.artifact_table.setItem(row, col, QTableWidgetItem(""))
        self.artifact_table.setCurrentCell(row, 0)

    def _remove_selected_artifact(self) -> None:
        rows = sorted({index.row() for index in self.artifact_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.artifact_table.removeRow(row)

    def _reset_default_artifacts(self) -> None:
        self._load_artifact_table(default_artifact_rules())

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
        self.database_skip_keys = set()
        self.selection_status.setText("No skip list loaded")
        self._log(f"Complete: {result.total_count} variants, {len(result.included)} included, {len(result.excluded)} excluded")
        self._refresh_metrics()
        self._refresh_variant_table()
        self.search_btn.setEnabled(True)
        self.browser_review_btn.setEnabled(True)
        self.rewrite_btn.setEnabled(True)
        self.patient_excel_btn.setEnabled(True)
        self.patient_pdf_btn.setEnabled(True)
        self.load_selection_btn.setEnabled(True)
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
        variants = self._variants_for_search()
        self._log(f"Search scope: {len(variants)}/{self.result.total_count} variants")
        browser_databases = self._selected_browser_databases(api_fallback_only=True)
        api_databases = [
            database for database in databases if database not in browser_databases
        ]
        self.evidence = {}
        worker = DatabaseWorker(
            variants,
            api_databases,
            browser_databases,
            self._browser_artifact_root(),
            self.settings,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status.connect(self._log)
        worker.patient_finished.connect(self._database_patient_finished)
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
        self._auto_rewrite_workbook()
        self._set_ready()
        self._log("Patient-by-patient evidence search complete")

    def _database_patient_finished(self, patient_evidence: dict) -> None:
        _merge_evidence_results(self.evidence, patient_evidence)
        self._refresh_evidence_table()
        self._auto_rewrite_workbook()

    def _start_browser_login(self) -> None:
        database = self.browser_database_combo.currentText()
        self._save_settings(silent=True)
        self._set_busy(f"{database} sign-in")
        worker = BrowserLoginWorker(database, self.settings)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status.connect(self._log)
        worker.finished.connect(self._browser_login_finished)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.browser_thread = thread
        self.browser_worker = worker
        thread.start()

    def _browser_login_finished(self, message: str) -> None:
        self._log(message)
        self._set_ready()

    def _start_browser_review(self) -> None:
        if not self.result:
            return
        self._save_settings(silent=True)
        databases = self._selected_browser_databases()
        if not databases:
            QMessageBox.warning(
                self,
                "No browser sources",
                "Select at least one of COSMIC, OncoKB, Franklin, or MTBP.",
            )
            return
        self._launch_browser_review(databases)

    def _selected_browser_databases(self, *, api_fallback_only: bool = False) -> list[str]:
        databases = [
            database
            for database in BROWSER_DATABASES
            if self.db_checks[database].isChecked()
        ]
        if not api_fallback_only:
            return databases
        return [
            database for database in databases
            if not (
                (database == "OncoKB" and self.settings.oncokb_api_key)
                or (database == "Franklin" and self.settings.franklin_api_key)
            )
        ]

    def _launch_browser_review(self, databases: list[str]) -> None:
        if not self.result:
            return
        self._set_busy("Browser lookups")
        worker = BrowserReviewWorker(
            self._variants_for_search(),
            databases,
            self._browser_artifact_root(),
            self.settings,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status.connect(self._log)
        worker.patient_finished.connect(self._browser_patient_finished)
        worker.finished.connect(self._browser_review_finished)
        worker.failed.connect(self._browser_review_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.browser_thread = thread
        self.browser_worker = worker
        thread.start()

    def _browser_artifact_root(self) -> Path:
        if not self.result:
            return Path(self.settings.default_output_dir) / "archer_browser_evidence"
        output_parent = (
            self.result.output_path.parent
            if self.result.output_path
            else Path(self.settings.default_output_dir)
        )
        output_stem = self.result.output_path.stem if self.result.output_path else "archer"
        return output_parent / f"{output_stem}_browser_evidence"

    def _variants_for_search(self):
        if not self.result:
            return []
        variants = (
            self.result.included
            if self.included_only_check.isChecked()
            else self.result.variants
        )
        return [
            variant
            for variant in variants
            if BrowserReviewService.variant_key(variant) not in self.database_skip_keys
        ]

    def _load_database_selection(self) -> None:
        if not self.result:
            return
        initial = str(self.result.output_path or self.settings.default_output_dir)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load reviewed database selections",
            initial,
            "Excel workbook (*.xlsx)",
        )
        if not path:
            return
        try:
            loaded = load_database_skip_keys(Path(path))
        except Exception as exc:
            QMessageBox.critical(self, "Selection workbook could not be loaded", str(exc))
            return
        available = {
            BrowserReviewService.variant_key(variant)
            for variant in self.result.variants
        }
        self.database_skip_keys = loaded & available
        unmatched = len(loaded - available)
        self.included_only_check.setChecked(False)
        searched = len(self.result.variants) - len(self.database_skip_keys)
        message = (
            f"{len(self.database_skip_keys)} marked X; {searched} variants will be searched"
        )
        if unmatched:
            message += f" ({unmatched} marks did not match this run)"
        self.selection_status.setText(message)
        self._log(f"Database selection loaded: {message}")

    def _browser_review_finished(self, browser_evidence: dict) -> None:
        self._merge_browser_evidence(browser_evidence)
        self._refresh_evidence_table()
        self._auto_rewrite_workbook()
        self._set_ready()
        self._log("Browser evidence lookup complete")

    def _browser_patient_finished(self, patient_evidence: dict) -> None:
        self._merge_browser_evidence(patient_evidence)
        self._refresh_evidence_table()
        self._auto_rewrite_workbook()

    def _merge_browser_evidence(self, browser_evidence: dict) -> None:
        _merge_evidence_results(self.evidence, browser_evidence)

    def _browser_review_failed(self, message: str) -> None:
        worker = self.browser_worker
        if isinstance(worker, BrowserReviewWorker):
            failed_evidence = {}
            for variant in worker.variants:
                key = BrowserReviewService.variant_key(variant)
                failed_evidence[key] = [
                    DatabaseEvidence(
                        database,
                        "error",
                        "Login-based lookup failed before a final result was captured. "
                        f"Details: {message}",
                    )
                    for database in worker.databases
                ]
            self._merge_browser_evidence(failed_evidence)
        self._refresh_evidence_table()
        self._auto_rewrite_workbook()
        self._worker_failed(message)

    def _rewrite_workbook(self) -> None:
        if not self.result or not self.result.output_path:
            return
        self._write_evidence_workbook()
        QMessageBox.information(self, "Workbook Updated", f"Evidence written to:\n{self.result.output_path}")

    def _export_patient_excels(self) -> None:
        if not self.result:
            return
        default_parent = (
            self.result.output_path.parent
            if self.result.output_path
            else Path(self.settings.default_output_dir)
        )
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select folder for patient Excel reports",
            str(default_parent),
        )
        if not selected:
            return
        output_stem = self.result.output_path.stem if self.result.output_path else "archer"
        report_directory = Path(selected) / f"{output_stem}_patient_excel_reports"
        try:
            outputs = PatientExcelReportWriter().write_all(
                self.result,
                report_directory,
                self.evidence,
                variants=self._variants_for_search(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "Patient Excel export failed", str(exc))
            return
        if not outputs:
            QMessageBox.warning(
                self,
                "No patient Excel reports created",
                "No patients had variants with decision 'included'.",
            )
            return
        self._log(
            f"Created {len(outputs)} patient Excel report(s) in {report_directory}"
        )
        QMessageBox.information(
            self,
            "Patient Excel reports created",
            f"Created {len(outputs)} report(s):\n{report_directory}",
        )

    def _export_patient_pdfs(self) -> None:
        if not self.result:
            return
        default_parent = (
            self.result.output_path.parent
            if self.result.output_path
            else Path(self.settings.default_output_dir)
        )
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select folder for patient PDF reports",
            str(default_parent),
        )
        if not selected:
            return
        output_stem = self.result.output_path.stem if self.result.output_path else "archer"
        report_directory = Path(selected) / f"{output_stem}_patient_reports"
        try:
            outputs = PatientPdfReportWriter().write_all(
                self.result,
                report_directory,
                self.evidence,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Patient PDF export failed", str(exc))
            return
        if not outputs:
            QMessageBox.warning(
                self,
                "No patient PDFs created",
                "No patients had variants with decision 'included'.",
            )
            return
        self._log(f"Created {len(outputs)} patient PDF report(s) in {report_directory}")
        QMessageBox.information(
            self,
            "Patient PDFs created",
            f"Created {len(outputs)} report(s):\n{report_directory}",
        )

    def _write_evidence_workbook(self) -> None:
        if not self.result or not self.result.output_path:
            return
        ExcelReportWriter().write(
            self.result,
            self.result.output_path,
            self.evidence,
            self.hide_excluded.isChecked(),
            self.database_skip_keys,
        )

    def _auto_rewrite_workbook(self) -> None:
        try:
            self._write_evidence_workbook()
            if self.result and self.result.output_path:
                self._log(f"Evidence workbook updated: {self.result.output_path}")
        except Exception as exc:
            self._log(
                "Could not update the workbook automatically. Close it in Excel "
                f"and use Rewrite Workbook With Evidence. ({exc})"
            )

    def _worker_failed(self, message: str) -> None:
        self._set_ready()
        self._log(f"ERROR: {message}")
        QMessageBox.critical(self, "Error", message)

    def _save_settings(self, silent: bool = False) -> None:
        self.settings.history_workbook = self.history_edit.text()
        self.settings.default_output_dir = self.output_dir_edit.text()
        self.settings.clinvar_api_key = self.clinvar_key_edit.text()
        self.settings.cosmic_email = self.cosmic_email_edit.text()
        self.settings.cosmic_password = self.cosmic_password_edit.text()
        self.settings.oncokb_api_key = self.oncokb_key_edit.text()
        self.settings.oncokb_email = self.oncokb_email_edit.text()
        self.settings.oncokb_password = self.oncokb_password_edit.text()
        self.settings.franklin_api_key = self.franklin_key_edit.text()
        self.settings.franklin_email = self.franklin_email_edit.text()
        self.settings.franklin_password = self.franklin_password_edit.text()
        self.settings.mtbp_email = self.mtbp_email_edit.text()
        self.settings.mtbp_password = self.mtbp_password_edit.text()
        self.settings.database_workers = self.worker_count.value()
        self.settings.browser_delay_seconds = self.browser_delay_spin.value()
        self.settings.browser_delay_max_seconds = max(
            self.settings.browser_delay_seconds,
            self.browser_delay_max_spin.value(),
        )
        self.browser_delay_max_spin.setValue(
            self.settings.browser_delay_max_seconds
        )
        self.settings.mtbp_timeout_minutes = self.mtbp_timeout_spin.value()
        self.settings.search_included_only = self.included_only_check.isChecked()
        self.settings.mtbp_cancer_type = self.mtbp_cancer_type_edit.text().strip() or "Blood"
        self.settings.artifact_rules = self._artifact_rules_from_table()
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
                highlight = variant_highlight(variant)
                if highlight == "artifact":
                    item.setBackground(QColor(Palette.pale_orange))
                elif highlight == "tier":
                    item.setBackground(QColor(Palette.pale_yellow))
                elif highlight == "germline":
                    item.setBackground(QColor(Palette.pale_green))
                self.variant_table.setItem(row, col, item)

    def _refresh_evidence_table(self) -> None:
        self.evidence_table.setRowCount(0)
        if not self.result:
            return
        evidence_by_key = self.evidence or {}
        variants = [
            variant
            for variant in self.result.variants
            if f"{variant.sample}|{variant.hgvsc}" in evidence_by_key
        ]
        for variant in variants:
            row = self.evidence_table.rowCount()
            self.evidence_table.insertRow(row)
            evidence_items = evidence_by_key.get(f"{variant.sample}|{variant.hgvsc}", [])
            by_database: dict[str, list] = {}
            for evidence in evidence_items:
                by_database.setdefault(evidence.database, []).append(evidence)
            values = [
                variant.sample,
                variant.symbol,
                variant.hgvsc,
                *[self._database_cell(by_database.get(database, [])) for database in self.databases],
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.evidence_table.setItem(row, col, item)

    def _set_busy(self, label: str) -> None:
        self.status_badge.setText(label)
        self.status_badge.setStyleSheet(
            f"background: {Palette.pale_blue}; color: {Palette.blue}; "
            f"border: 1px solid {Palette.blue}; border-radius: 12px; "
            "padding: 5px 12px; font-weight: 700;"
        )
        self.activity_progress.show()
        self.process_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.browser_signin_btn.setEnabled(False)
        self.browser_review_btn.setEnabled(False)
        self.rewrite_btn.setEnabled(False)
        self.patient_pdf_btn.setEnabled(False)
        self.patient_excel_btn.setEnabled(False)
        self.status_bar.showMessage(label)

    def _set_ready(self) -> None:
        self.status_badge.setText("Ready")
        self.status_badge.setStyleSheet("")
        self.activity_progress.hide()
        self._update_process_state()
        self.search_btn.setEnabled(self.result is not None)
        self.browser_signin_btn.setEnabled(True)
        self.browser_review_btn.setEnabled(self.result is not None)
        self.rewrite_btn.setEnabled(self.result is not None)
        self.patient_pdf_btn.setEnabled(self.result is not None)
        self.patient_excel_btn.setEnabled(self.result is not None)
        self.status_bar.showMessage("Ready", 3000)

    def _update_process_state(self) -> None:
        has_input = Path(self.input_edit.text()).exists()
        has_output = bool(self.output_edit.text().strip())
        self.process_btn.setEnabled(has_input and has_output)

    def _log(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.status_bar.showMessage(message, 5000)

    def _table_text(self, table: QTableWidget, row: int, col: int) -> str:
        item = table.item(row, col)
        return item.text().strip() if item else ""

    def _database_cell(self, evidence_items: list) -> str:
        return "\n".join(f"[{item.status}] {item.summary}".strip() for item in evidence_items)

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
                border-radius: 10px;
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
            QLineEdit, QDateEdit, QPlainTextEdit, QTableWidget, QComboBox, QSpinBox {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 7px;
                padding: 6px;
                selection-background-color: {Palette.pale_blue};
                selection-color: {Palette.navy};
            }}
            QLineEdit:focus, QDateEdit:focus, QPlainTextEdit:focus,
            QTableWidget:focus, QComboBox:focus, QSpinBox:focus {{
                border: 1px solid {Palette.blue};
            }}
            QPushButton {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 7px;
                padding: 8px 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border-color: {Palette.blue};
                background: {Palette.pale_blue};
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
            QPushButton#PrimaryButton:hover {{
                background: #245F93;
            }}
            QPushButton#OutlineButton {{
                background: {Palette.pale_blue};
                color: {Palette.navy};
                border-color: {Palette.blue};
            }}
            QPushButton#ReportButton {{
                background: {Palette.green};
                color: white;
                border-color: {Palette.green};
            }}
            QPushButton#ReportButton:hover {{
                background: #3F724A;
            }}
            QFrame#MetricCard {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 10px;
            }}
            QFrame#WorkflowStep {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 9px;
            }}
            QLabel#StepNumber {{
                background: {Palette.navy};
                color: white;
                border-radius: 15px;
                font-weight: 700;
            }}
            QLabel#StepTitle {{
                color: {Palette.navy};
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#StepDescription, QLabel#HelperText {{
                color: {Palette.muted};
                font-size: 12px;
            }}
            QLabel#PageIntro {{
                background: {Palette.pale_blue};
                color: {Palette.navy};
                border: 1px solid {Palette.border};
                border-radius: 8px;
                padding: 10px 12px;
            }}
            QLabel#FieldLabel {{
                color: {Palette.navy};
                font-weight: 700;
            }}
            QLabel#SecurityNote {{
                background: {Palette.pale_green};
                color: {Palette.green};
                border-radius: 6px;
                padding: 7px 9px;
                font-size: 12px;
            }}
            QCheckBox[loginSource="true"] {{
                color: {Palette.blue};
                font-weight: 600;
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
                border-radius: 10px;
            }}
            QTabBar::tab {{
                padding: 10px 20px;
                background: #E8EEF3;
                border: 1px solid {Palette.border};
                border-bottom: none;
                margin-right: 2px;
            }}
            QTabBar::tab:hover {{
                color: {Palette.blue};
                background: {Palette.pale_blue};
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
            QTableWidget {{
                gridline-color: #E4EBF0;
                alternate-background-color: #F7FAFC;
            }}
            QTableWidget::item:selected {{
                background: {Palette.pale_blue};
                color: {Palette.navy};
            }}
            QProgressBar#ActivityProgress {{
                background: #E8EEF3;
                border: none;
                border-radius: 3px;
                min-height: 6px;
                max-height: 6px;
            }}
            QProgressBar#ActivityProgress::chunk {{
                background: {Palette.blue};
                border-radius: 3px;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 10px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: #B9C8D3;
                border-radius: 4px;
                min-height: 28px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            """
        )


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
