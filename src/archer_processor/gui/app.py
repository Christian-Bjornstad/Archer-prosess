from __future__ import annotations

import sys
import random
import threading
import time
from copy import deepcopy
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QDate, QObject, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QToolButton,
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
    QSizePolicy,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from archer_processor.core import DatabaseEvidence, FilterEngine, ProcessingResult, VariantProcessor, default_artifact_rules, production_rules
from archer_processor.core.highlights import (
    is_automatic_database_skip,
)
from archer_processor.core.run_date import sequencing_date_from_path
from archer_processor.io import ArcherTsvReader
from archer_processor.reports import (
    ExcelReportWriter,
    PatientExcelReportWriter,
    PatientReportCoordinator,
    PatientReportOutcome,
)
from archer_processor.reports.who_genes import load_who_driver_genes
from archer_processor.services.artifact_catalog import load_artifact_rules
from archer_processor.services.credentials import CredentialStoreError
from archer_processor.services import (
    AppSettings,
    BROWSER_DATABASES,
    BrowserReviewCancelled,
    BrowserReviewService,
    DatabaseSearchService,
    ProcessedWorkbookLoader,
    RETRYABLE_EVIDENCE_STATUSES,
    is_completed_evidence,
    inspect_recent_analysis,
    load_database_skip_keys,
)
from archer_processor.gui.status_model import (
    RunActivity,
    RunPhase,
    RunSnapshot,
    build_patient_status_rows,
)
from archer_processor.gui.theme import Palette, application_stylesheet
from archer_processor.gui.widgets.navigation import NavigationRail
from archer_processor.gui.widgets.run_status import RunStatusStrip
from archer_processor.gui.widgets.status_matrix import StatusMatrix
from archer_processor.gui.widgets.patient_details import PatientDetailsDialog
from archer_processor.gui.widgets.file_drop import AnalysisFileDrop
from archer_processor import __version__
from archer_processor.services.run_journal import RunJournal


AUTOMATIC_RETRYABLE_EVIDENCE_STATUSES = frozenset(
    {"error", "timeout", "session_lost", "partial_capture"}
)


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
            self.status.emit("Reading variant TSV")
            filter_engine = FilterEngine(production_rules(load_artifact_rules(
                self.settings.artifact_rules_path, fallback=self.settings.artifact_rules,
            )))
            processor = VariantProcessor(filter_engine=filter_engine)
            result = processor.process(self.input_path, self.run_date, self.output_path)
            self.status.emit(f"Archer version detected: {result.archer_version} (TSV headers)")
            for warning in result.warnings:
                self.status.emit(warning)
            self.status.emit("Writing review workbook")
            ExcelReportWriter(
                load_who_driver_genes(self.settings.who_driver_genes_path)
            ).write(result, self.output_path, hide_excluded=self.hide_excluded)
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class ProcessedWorkbookWorker(QObject):
    finished = pyqtSignal(object, object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, workbook_path: Path, settings: AppSettings):
        super().__init__()
        self.workbook_path = workbook_path
        self.settings = settings

    def run(self) -> None:
        try:
            state = ProcessedWorkbookLoader(
                filter_engine=FilterEngine(
                    production_rules(load_artifact_rules(
                        self.settings.artifact_rules_path, fallback=self.settings.artifact_rules,
                    ))
                ),
            ).load(self.workbook_path, progress=self.progress.emit)
            self.finished.emit(self.workbook_path, state)
        except Exception as exc:
            self.failed.emit(str(exc))


class PatientReportWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        coordinator: PatientReportCoordinator,
        patient_ids: list[str],
    ) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.patient_ids = list(patient_ids)

    def run(self) -> None:
        try:
            outcomes = []
            total = len(self.patient_ids)
            for current, patient_id in enumerate(self.patient_ids, start=1):
                outcomes.append(self.coordinator.write_patient(patient_id))
                self.progress.emit(current, total, patient_id)
            self.finished.emit(outcomes)
        except Exception as exc:
            self.failed.emit(str(exc))


class ReportRetryWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, coordinator: PatientReportCoordinator) -> None:
        super().__init__()
        self.coordinator = coordinator

    def run(self) -> None:
        try:
            self.finished.emit(self.coordinator.retry_pending())
        except Exception as exc:
            self.failed.emit(str(exc))


def _variants_grouped_by_patient(variants) -> list[tuple[str, list]]:
    grouped: dict[str, list] = {}
    for variant in variants:
        grouped.setdefault(variant.patient_id, []).append(variant)
    return list(grouped.items())


def _browser_database_lanes(databases: list[str]) -> list[tuple[str, list[str]]]:
    requested = set(databases)
    fast_databases = [
        database
        for database in BROWSER_DATABASES
        if database in requested and database not in {"ClinVar", "Franklin", "MTBP"}
    ]
    lanes: list[tuple[str, list[str]]] = []
    if fast_databases:
        lanes.append(("fast databases", fast_databases))
    if "ClinVar" in requested:
        lanes.append(("ClinVar", ["ClinVar"]))
    if "Franklin" in requested:
        lanes.append(("Franklin", ["Franklin"]))
    if "MTBP" in requested:
        lanes.append(("MTBP", ["MTBP"]))
    return lanes


def _merge_evidence_results(target: dict, incoming: dict) -> None:
    for key, new_items in incoming.items():
        by_database = {item.database: item for item in target.get(key, [])}
        by_database.update({item.database: item for item in new_items})
        target[key] = list(by_database.values())


def _evidence_completion_summary(
    evidence: dict[str, list[DatabaseEvidence]],
) -> str:
    counts = {
        "found_complete": 0,
        "found_incomplete": 0,
        "not_found": 0,
        "not_applicable": 0,
        "manual_review": 0,
        "deferred": 0,
        "unfinished": 0,
    }
    for items in evidence.values():
        for item in items:
            status = item.status.strip().casefold()
            if status == "found":
                counts[
                    "found_complete" if is_completed_evidence(item) else "found_incomplete"
                ] += 1
            elif status == "not_found":
                counts["not_found"] += 1
            elif status == "not_applicable":
                counts["not_applicable"] += 1
            elif status in {"manual", "manual_review"}:
                counts["manual_review"] += 1
            elif status == "deferred":
                counts["deferred"] += 1
            elif not is_completed_evidence(item):
                counts["unfinished"] += 1
    counts["total"] = sum(counts.values())
    return " | ".join(
        ["RUN SUMMARY", *(f"{key}={value}" for key, value in counts.items())]
    )


def _completed_evidence_sources(
    evidence: dict[str, list[DatabaseEvidence]],
) -> set[tuple[str, str]]:
    completed: set[tuple[str, str]] = set()
    for key, items in evidence.items():
        for item in items:
            if is_completed_evidence(item):
                completed.add((key, item.database))
    return completed


def _pending_source_counts(
    variants,
    databases: list[str],
    completed_sources: set[tuple[str, str]],
) -> dict[str, int]:
    return {
        database: sum(
            (BrowserReviewService.variant_key(variant), database)
            not in completed_sources
            for variant in variants
        )
        for database in databases
    }


def _protected_remote_evidence_sources(
    evidence: dict[str, list[DatabaseEvidence]],
) -> set[tuple[str, str]]:
    """Remote reports whose IDs must survive a generic worker failure."""
    protected: set[tuple[str, str]] = set()
    for key, items in evidence.items():
        for item in items:
            analysis_id = str(item.raw.get("analysis_id") or "")
            if (
                item.database == "MTBP"
                and analysis_id.startswith("ARCHER-")
                and not is_completed_evidence(item)
            ):
                protected.add((key, item.database))
    return protected


def _failed_search_variants(
    variants,
    evidence: dict[str, list[DatabaseEvidence]],
    databases: list[str],
) -> list:
    """Variants with at least one requested source stuck in a retryable state.

    A variant counts as failed only when an actual lookup attempt produced an
    evidence record with a retryable status (error, timeout, session_lost, ...).
    Variants that were never attempted are left alone.
    """
    failed: list = []
    for variant in variants:
        key = BrowserReviewService.variant_key(variant)
        by_database = {item.database: item for item in evidence.get(key, [])}
        if any(
            database in by_database
            and by_database[database].status.strip().casefold()
            in AUTOMATIC_RETRYABLE_EVIDENCE_STATUSES
            for database in databases
        ):
            failed.append(variant)
    return failed


def _retryable_source_pairs(
    variants,
    evidence: dict[str, list[DatabaseEvidence]],
    databases: list[str],
) -> set[tuple[str, str]]:
    """Attempted lookups a user can explicitly retry for these variants."""
    selected = set(databases)
    pairs: set[tuple[str, str]] = set()
    for variant in variants:
        key = BrowserReviewService.variant_key(variant)
        for item in evidence.get(key, []):
            if item.database not in selected:
                continue
            status = item.status.strip().casefold()
            if status in RETRYABLE_EVIDENCE_STATUSES or (
                status == "found" and not is_completed_evidence(item)
            ):
                pairs.add((key, item.database))
    return pairs


def _has_failed_lookups(
    variants,
    evidence: dict[str, list[DatabaseEvidence]],
    databases: list[str],
) -> bool:
    return bool(_failed_search_variants(variants, evidence, databases))


class SearchPauseControl:
    """Thread-safe cooperative pause gate shared with a search worker."""

    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()

    @property
    def pause_requested(self) -> bool:
        return not self._resume.is_set()

    def request_pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def wait(
        self,
        *,
        stop_requested: Callable[[], bool],
        pause_changed: Callable[[bool], None],
    ) -> None:
        if self._resume.is_set():
            return
        pause_changed(True)
        try:
            while not self._resume.wait(0.1):
                if stop_requested():
                    raise BrowserReviewCancelled("Evidence search stopped by user.")
        finally:
            pause_changed(False)
        if stop_requested():
            raise BrowserReviewCancelled("Evidence search stopped by user.")


class DatabaseWorker(QObject):
    finished = pyqtSignal(object)
    cancelled = pyqtSignal()
    patient_finished = pyqtSignal(object)
    evidence_updated = pyqtSignal(object)
    source_state = pyqtSignal(str, str, bool)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)
    paused = pyqtSignal(bool)
    report_outcome = pyqtSignal(object)

    def __init__(
        self,
        variants,
        api_databases: list[str],
        browser_databases: list[str],
        artifact_root: Path,
        settings: AppSettings,
        completed_sources: set[tuple[str, str]] | None = None,
        patient_indexes: dict[str, int] | None = None,
        result: ProcessingResult | None = None,
        existing_evidence: dict[str, list[DatabaseEvidence]] | None = None,
        report_variants=None,
    ):
        super().__init__()
        self.variants = variants
        self.api_databases = api_databases
        self.browser_databases = browser_databases
        self.databases = [*api_databases, *browser_databases]
        self.artifact_root = artifact_root
        self.settings = settings
        self.completed_sources = set(completed_sources or set())
        self.patient_indexes = dict(patient_indexes or {})
        self.pause_control = SearchPauseControl()
        self.result = result
        self.existing_evidence = existing_evidence or {}
        self.report_variants = list(report_variants or variants)
        self._owner_thread: QThread | None = None

    def run(self) -> None:
        try:
            self._owner_thread = QThread.currentThread()
            api_service = DatabaseSearchService(self.settings)
            browser_services = {
                lane_name: self._browser_service()
                for lane_name, _ in self._browser_lanes()
            }
            for database, status in api_service.database_diagnostics(self.api_databases).items():
                self.status.emit(f"{database}: {status}")
            for database in self.browser_databases:
                self.status.emit(f"{database}: website lookup in Microsoft Edge")
            patients = _variants_grouped_by_patient(self.variants)
            all_evidence: dict[str, list[DatabaseEvidence]] = {}
            self.status.emit(
                f"Patient-by-patient search started: {len(patients)} patients, "
                f"{len(self.databases)} sources"
            )
            self.progress.emit(0, len(patients), "Preparing patient queue")
            original_patient_total = max(
                self.patient_indexes.values(), default=len(patients)
            )
            for patient_index, (patient_id, patient_variants) in enumerate(patients, start=1):
                self._check_cancelled()
                patient_evidence: dict[str, list[DatabaseEvidence]] = {
                    api_service.variant_key(variant): [] for variant in patient_variants
                }
                original_patient_index = self.patient_indexes.get(
                    patient_id, patient_index
                )
                prefix = (
                    f"Patient {original_patient_index}/{original_patient_total} "
                    f"({patient_id})"
                )
                self.progress.emit(
                    patient_index - 1,
                    len(patients),
                    f"{patient_id} · {len(patient_variants)} variant(s) · {len(self.databases)} source(s)",
                )
                self.status.emit(f"{prefix}: starting {len(patient_variants)} variant(s)")
                for database in self.api_databases:
                    self._check_cancelled()
                    pending_variants = [
                        variant
                        for variant in patient_variants
                        if (
                            api_service.variant_key(variant),
                            database,
                        ) not in self.completed_sources
                    ]
                    if not pending_variants:
                        self.status.emit(f"{prefix}: {database} already complete; skipped")
                        continue
                    self.status.emit(
                        f"{prefix}: searching {database} for "
                        f"{len(pending_variants)} pending variant(s)"
                    )
                    self.source_state.emit(patient_id, database, True)
                    database_evidence: dict[str, list[DatabaseEvidence]] = {}
                    for variant in pending_variants:
                        self._check_cancelled()
                        key = api_service.variant_key(variant)
                        try:
                            database_evidence[key] = api_service.search_variant(
                                variant, [database]
                            )
                        except Exception as exc:
                            database_evidence[key] = [
                                DatabaseEvidence(database, "error", str(exc))
                            ]
                        self.evidence_updated.emit({key: deepcopy(database_evidence[key])})
                    _merge_evidence_results(patient_evidence, database_evidence)
                    self.source_state.emit(patient_id, database, False)

                if self.browser_databases:
                    if patient_index > 1:
                        self._wait(
                            f"{prefix}: website safety buffer before signed-in sources"
                        )
                    browser_evidence = self._search_browser_lanes(
                        patient_id,
                        patient_variants,
                        original_patient_index,
                        prefix,
                        services=browser_services,
                    )
                    _merge_evidence_results(patient_evidence, browser_evidence)

                _merge_evidence_results(all_evidence, patient_evidence)
                self.patient_finished.emit(patient_evidence)
                self.status.emit(f"{prefix}: complete")
                self.progress.emit(
                    patient_index,
                    len(patients),
                    f"Completed {patient_id}",
                )
            self.finished.emit(all_evidence)
        except BrowserReviewCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


    def _browser_lanes(self) -> list[tuple[str, list[str]]]:
        return _browser_database_lanes(self.browser_databases)

    def _search_browser_lanes(
        self,
        patient_id: str,
        patient_variants: list,
        original_patient_index: int,
        prefix: str,
        *,
        services: dict[str, BrowserReviewService] | None = None,
    ) -> dict[str, list[DatabaseEvidence]]:
        lanes = self._browser_lanes()
        if not lanes:
            return {}
        active_services = services or {
            lane_name: self._browser_service() for lane_name, _ in lanes
        }
        patient_evidence: dict[str, list[DatabaseEvidence]] = {}
        if len(lanes) == 1:
            lane_name, databases = lanes[0]
            result = self._run_browser_lane(
                lane_name,
                databases,
                active_services[lane_name],
                patient_id,
                patient_variants,
                original_patient_index,
                prefix,
            )
            _merge_evidence_results(patient_evidence, result)
            return patient_evidence

        with ThreadPoolExecutor(
            max_workers=len(lanes), thread_name_prefix="database-browser"
        ) as executor:
            futures = [
                executor.submit(
                    self._run_browser_lane,
                    lane_name,
                    databases,
                    active_services[lane_name],
                    patient_id,
                    patient_variants,
                    original_patient_index,
                    prefix,
                )
                for lane_name, databases in lanes
            ]
            for future in as_completed(futures):
                _merge_evidence_results(patient_evidence, future.result())
        return patient_evidence

    def _run_browser_lane(
        self,
        lane_name: str,
        databases: list[str],
        service: BrowserReviewService,
        patient_id: str,
        patient_variants: list,
        original_patient_index: int,
        prefix: str,
    ) -> dict[str, list[DatabaseEvidence]]:
        started_at = time.monotonic()
        result = service.search_variants(
            patient_variants,
            databases,
            self.artifact_root / f"patient-{original_patient_index:03d}",
            progress=lambda message, p=prefix, lane=lane_name: self.status.emit(
                f"{p} · {lane}: {message}"
            ),
            completed_sources=self.completed_sources,
            checkpoint=self.evidence_updated.emit,
            activity=lambda database, action: self.source_state.emit(
                patient_id, database, action == "Starting provider"
            ) if action in {"Starting provider", "Provider finished"} else None,
            prior_evidence=self.existing_evidence,
        )
        elapsed = time.monotonic() - started_at
        self.status.emit(f"{prefix}: {lane_name} complete ({elapsed:.1f}s)")
        return result

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
            stop_requested=self._stop_requested,
            pause_wait=self._wait_if_paused,
            browser_background=self.settings.browser_background,
        )

    def request_pause(self) -> None:
        self.pause_control.request_pause()

    def resume_search(self) -> None:
        self.pause_control.resume()

    def _wait_if_paused(self) -> None:
        self.pause_control.wait(
            stop_requested=self._stop_requested,
            pause_changed=self.paused.emit,
        )

    def _stop_requested(self) -> bool:
        thread = self._owner_thread or QThread.currentThread()
        return thread.isInterruptionRequested()

    def _check_cancelled(self) -> None:
        if self._stop_requested():
            raise BrowserReviewCancelled("Evidence search stopped by user.")
        self._wait_if_paused()

    def _wait(self, reason: str) -> None:
        minimum = max(0, int(self.settings.browser_delay_seconds))
        maximum = max(minimum, int(self.settings.browser_delay_max_seconds))
        delay = random.randint(minimum, maximum)
        if delay <= 0:
            return
        self.status.emit(f"{reason}: {delay}s")
        remaining = float(delay)
        while remaining > 0:
            self._check_cancelled()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk


class WorkbookWriteWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(object)

    def __init__(
        self,
        result: ProcessingResult,
        evidence: dict[str, list[DatabaseEvidence]],
        *,
        hide_excluded: bool,
        database_skip_keys: set[str],
        who_driver_genes_path: str = "",
    ) -> None:
        super().__init__()
        self.result = deepcopy(result)
        self.evidence = deepcopy(evidence)
        self.hide_excluded = bool(hide_excluded)
        self.database_skip_keys = set(database_skip_keys)
        self.who_driver_genes_path = who_driver_genes_path

    def run(self) -> None:
        try:
            if self.result.output_path is None:
                raise RuntimeError("Evidence workbook output path is missing.")
            path = ExcelReportWriter(load_who_driver_genes(self.who_driver_genes_path)).write(
                self.result,
                self.result.output_path,
                self.evidence,
                self.hide_excluded,
                self.database_skip_keys,
            )
            self.finished.emit(path)
        except Exception as exc:
            self.failed.emit(exc)


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
                browser_background=self.settings.browser_background,
            )
            self.finished.emit(service.open_login(self.database))
        except Exception as exc:
            self.failed.emit(str(exc))


class BrowserSessionCheckWorker(QObject):
    checked = pyqtSignal(object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, database: str | None = None):
        super().__init__()
        self.database = database

    def run(self) -> None:
        try:
            from archer_processor.services.browser_sessions import BrowserSessionCheckService

            service = BrowserSessionCheckService(BrowserReviewService(
                browser_background=True,
                stop_requested=QThread.currentThread().isInterruptionRequested,
            ))
            if self.database is None:
                results = service.check_all(on_result=self.checked.emit)
            else:
                result = service.check(self.database)
                self.checked.emit(result)
                results = [result]
            self.finished.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class BrowserReviewWorker(QObject):
    finished = pyqtSignal(object)
    cancelled = pyqtSignal()
    patient_finished = pyqtSignal(object)
    evidence_updated = pyqtSignal(object)
    source_state = pyqtSignal(str, str, bool)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)
    paused = pyqtSignal(bool)
    report_outcome = pyqtSignal(object)
    activity = pyqtSignal(object)

    def __init__(
        self,
        variants,
        databases: list[str],
        artifact_root: Path,
        settings: AppSettings,
        completed_sources: set[tuple[str, str]] | None = None,
        patient_indexes: dict[str, int] | None = None,
        result: ProcessingResult | None = None,
        existing_evidence: dict[str, list[DatabaseEvidence]] | None = None,
        report_variants=None,
    ):
        super().__init__()
        self.variants = variants
        self.databases = databases
        self.artifact_root = artifact_root
        self.settings = settings
        self.completed_sources = set(completed_sources or set())
        self.patient_indexes = dict(patient_indexes or {})
        self.pause_control = SearchPauseControl()
        self.result = result
        self.existing_evidence = existing_evidence or {}
        self.report_variants = list(report_variants or variants)
        # Evidence collected across all retry passes within this worker's run;
        # used so retry passes never repeat lookups that already succeeded.
        self._pass_evidence: dict[str, list[DatabaseEvidence]] = {}
        self._paused_database_reasons: dict[str, str] = {}
        self._consecutive_provider_failures: dict[str, int] = {}
        self._provider_state_lock = threading.Lock()
        self._owner_thread: QThread | None = None

    def run(self) -> None:
        try:
            self._owner_thread = QThread.currentThread()
            all_evidence: dict[str, list[DatabaseEvidence]] = {}
            patients = _variants_grouped_by_patient(self.variants)
            lane_services = {
                lane_name: self._build_service()
                for lane_name, _ in self._database_lanes()
            }
            self.progress.emit(0, len(patients), "Preparing signed-in browser queue")
            original_patient_total = max(
                self.patient_indexes.values(), default=len(patients)
            )
            for patient_index, (patient_id, patient_variants) in enumerate(patients, start=1):
                self._check_cancelled()
                original_patient_index = self.patient_indexes.get(
                    patient_id, patient_index
                )
                prefix = (
                    f"Patient {original_patient_index}/{original_patient_total} "
                    f"({patient_id})"
                )
                self.progress.emit(
                    patient_index - 1,
                    len(patients),
                    f"{patient_id} · {len(patient_variants)} variant(s) · {len(self.databases)} source(s)",
                )
                patient_evidence = self._search_patient_lanes(
                    patient_id,
                    patient_variants,
                    original_patient_index,
                    prefix,
                    services=lane_services,
                )
                _merge_evidence_results(all_evidence, patient_evidence)
                # Audit records are persisted by each provider result.  The much
                # heavier network-workbook checkpoint is intentionally coalesced
                # until all parallel lanes for this patient have finished.
                self.patient_finished.emit(patient_evidence)
                self.status.emit(f"{prefix}: browser sources complete")
                self.progress.emit(patient_index, len(patients), f"Completed {patient_id}")
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
                        remaining = float(delay)
                        while remaining > 0:
                            self._check_cancelled()
                            chunk = min(0.25, remaining)
                            time.sleep(chunk)
                            remaining -= chunk
            self.finished.emit(all_evidence)
        except BrowserReviewCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))

    def _database_lanes(self) -> list[tuple[str, list[str]]]:
        return _browser_database_lanes(self.databases)

    def _search_patient_lanes(
        self,
        patient_id: str,
        patient_variants: list,
        original_patient_index: int,
        prefix: str,
        *,
        services: dict[str, BrowserReviewService] | None = None,
    ) -> dict[str, list[DatabaseEvidence]]:
        with self._provider_state_lock:
            paused = dict(self._paused_database_reasons)
        lanes = [
            (lane_name, [database for database in databases if database not in paused])
            for lane_name, databases in self._database_lanes()
        ]
        lanes = [(lane_name, databases) for lane_name, databases in lanes if databases]
        deferred_evidence: dict[str, list[DatabaseEvidence]] = {}
        for variant in patient_variants:
            key = BrowserReviewService.variant_key(variant)
            deferred_evidence[key] = [
                DatabaseEvidence(
                    database,
                    "deferred",
                    f"{database} was paused for this run: {reason}",
                    raw={
                        "failure_stage": "provider_circuit_breaker",
                        "deferred_reason": reason,
                    },
                )
                for database, reason in paused.items()
                if database in self.databases
            ]
            for evidence in deferred_evidence[key]:
                self.status.emit(
                    f"{prefix}: RESULT | source={evidence.database} | "
                    f"variant={variant.hgvsc or variant.hgvsp} | status=deferred | "
                    "retryable=yes | stage=provider_circuit_breaker"
                )
        if not lanes:
            return deferred_evidence

        lane_states = {lane_name: {} for lane_name, _ in lanes}
        active_services = services or {
            lane_name: self._build_service() for lane_name, _ in lanes
        }
        patient_evidence: dict[str, list[DatabaseEvidence]] = {}
        _merge_evidence_results(patient_evidence, deferred_evidence)
        if len(lanes) == 1:
            lane_name, databases = lanes[0]
            result = self._run_patient_lane(
                lane_name,
                databases,
                active_services[lane_name],
                patient_id,
                patient_variants,
                original_patient_index,
                prefix,
                lane_states[lane_name],
            )
            _merge_evidence_results(patient_evidence, result)
        else:
            with ThreadPoolExecutor(
                max_workers=len(lanes), thread_name_prefix="browser-review"
            ) as executor:
                futures = {
                    executor.submit(
                        self._run_patient_lane,
                        lane_name,
                        databases,
                        active_services[lane_name],
                        patient_id,
                        patient_variants,
                        original_patient_index,
                        prefix,
                        lane_states[lane_name],
                    ): lane_name
                    for lane_name, databases in lanes
                }
                for future in as_completed(futures):
                    _merge_evidence_results(patient_evidence, future.result())

        for lane_evidence in lane_states.values():
            _merge_evidence_results(self._pass_evidence, lane_evidence)
        return patient_evidence

    def _run_patient_lane(
        self,
        lane_name: str,
        databases: list[str],
        service: BrowserReviewService,
        patient_id: str,
        patient_variants: list,
        original_patient_index: int,
        prefix: str,
        pass_evidence: dict[str, list[DatabaseEvidence]],
    ) -> dict[str, list[DatabaseEvidence]]:
        started_at = time.monotonic()
        result = self._search_patient_with_retries(
            service,
            patient_id,
            patient_variants,
            original_patient_index,
            f"{prefix} · {lane_name}",
            databases=databases,
            pass_evidence=pass_evidence,
        )
        unresolved_mtbp_report = next(
            (
                item
                for items in result.values()
                for item in items
                if item.database == "MTBP"
                and str(item.raw.get("analysis_id") or "").startswith("ARCHER-")
                and not is_completed_evidence(item)
            ),
            None,
        )
        if unresolved_mtbp_report is not None:
            reason = unresolved_mtbp_report.summary or "remote report is unresolved"
            with self._provider_state_lock:
                self._paused_database_reasons.setdefault("MTBP", reason)
            self.status.emit(
                f"{prefix}: PROVIDER PAUSED | source=MTBP | "
                "reason=unresolved remote report; later patients will be deferred"
            )
        oncokb_items = [
            item
            for items in result.values()
            for item in items
            if item.database == "OncoKB"
        ]
        if oncokb_items:
            failed = all(
                item.status.strip().casefold()
                in AUTOMATIC_RETRYABLE_EVIDENCE_STATUSES
                for item in oncokb_items
            )
            with self._provider_state_lock:
                consecutive = (
                    self._consecutive_provider_failures.get("OncoKB", 0) + 1
                    if failed
                    else 0
                )
                self._consecutive_provider_failures["OncoKB"] = consecutive
                if consecutive >= 2:
                    self._paused_database_reasons.setdefault(
                        "OncoKB",
                        "two consecutive patient-level rendering or provider failures",
                    )
            if failed and consecutive >= 2:
                self.status.emit(
                    f"{prefix}: PROVIDER PAUSED | source=OncoKB | "
                    "reason=two consecutive patient failures; later patients will be deferred"
                )
        elapsed = time.monotonic() - started_at
        self.status.emit(f"{prefix}: {lane_name} complete ({elapsed:.1f}s)")
        return result

    def _search_patient_with_retries(
        self,
        service: BrowserReviewService,
        patient_id: str,
        patient_variants: list,
        original_patient_index: int,
        prefix: str,
        *,
        databases: list[str] | None = None,
        pass_evidence: dict[str, list[DatabaseEvidence]] | None = None,
    ) -> dict[str, list[DatabaseEvidence]]:
        """Search one patient, then retry transient lookup failures once.

        The first pass collects everything; if any variant/source pair ends in a
        transient state (site down, timeout, lost session), a single immediate
        retry pass runs for just those lookups. Terminal identity and availability
        results are retained for a later user-initiated run.
        """
        active_databases = list(
            self.databases if databases is None else databases
        )
        accumulated_evidence = (
            pass_evidence if pass_evidence is not None else self._pass_evidence
        )
        patient_evidence = self._run_search_pass(
            service,
            patient_id,
            patient_variants,
            original_patient_index,
            prefix,
            databases=active_databases,
            pass_evidence=accumulated_evidence,
        )
        _merge_evidence_results(accumulated_evidence, patient_evidence)
        failed = _failed_search_variants(
            patient_variants, patient_evidence, active_databases
        )
        if not failed:
            return patient_evidence
        self.status.emit(
            f"{prefix}: {len(failed)} lookup(s) failed; retrying once"
        )
        retry_evidence = self._run_search_pass(
            service,
            patient_id,
            failed,
            original_patient_index,
            f"{prefix} (retry)",
            databases=active_databases,
            pass_evidence=accumulated_evidence,
        )
        _merge_evidence_results(patient_evidence, retry_evidence)
        _merge_evidence_results(accumulated_evidence, retry_evidence)
        still_failed = _failed_search_variants(
            failed, patient_evidence, active_databases
        )
        if still_failed:
            self.status.emit(
                f"{prefix}: {len(still_failed)} lookup(s) still failing after "
                "retry; start evidence search again later to retry them"
            )
        return patient_evidence

    def _run_search_pass(
        self,
        service: BrowserReviewService,
        patient_id: str,
        variants: list,
        original_patient_index: int,
        prefix: str,
        *,
        databases: list[str] | None = None,
        pass_evidence: dict[str, list[DatabaseEvidence]] | None = None,
    ) -> dict[str, list[DatabaseEvidence]]:
        active_databases = list(
            self.databases if databases is None else databases
        )
        accumulated_evidence = (
            pass_evidence if pass_evidence is not None else self._pass_evidence
        )
        completed_sources = self.completed_sources | _completed_evidence_sources(
            accumulated_evidence
        )
        prior_evidence = {
            key: list(items) for key, items in self.existing_evidence.items()
        }
        _merge_evidence_results(prior_evidence, accumulated_evidence)
        return service.search_variants(
            variants,
            active_databases,
            self.artifact_root / f"patient-{original_patient_index:03d}",
            progress=lambda message, p=prefix: self.status.emit(f"{p}: {message}"),
            activity=lambda database, message: self._provider_activity(
                patient_id, database, message, variants
            ),
            completed_sources=completed_sources,
            checkpoint=self.evidence_updated.emit,
            prior_evidence=prior_evidence,
        )

    def _provider_activity(self, patient_id, database, message, variants) -> None:
        if message in {"Starting provider", "Provider finished"}:
            self.source_state.emit(patient_id, database, message == "Starting provider")
        self.activity.emit(
            RunActivity(
                occurred_at=datetime.now(),
                patient_id=patient_id,
                database=database,
                variant_label=(
                    variants[0].display_name
                    if len(variants) == 1 else f"{len(variants)} variants"
                ),
                action=message,
                message=message,
            )
        )

    def _build_service(self) -> BrowserReviewService:
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
            stop_requested=self._stop_requested,
            pause_wait=self._wait_if_paused,
            browser_background=self.settings.browser_background,
        )

    def request_pause(self) -> None:
        self.pause_control.request_pause()

    def resume_search(self) -> None:
        self.pause_control.resume()

    def _wait_if_paused(self) -> None:
        self.pause_control.wait(
            stop_requested=self._stop_requested,
            pause_changed=self.paused.emit,
        )

    def _stop_requested(self) -> bool:
        thread = self._owner_thread or QThread.currentThread()
        return thread.isInterruptionRequested()

    def _check_cancelled(self) -> None:
        if self._stop_requested():
            raise BrowserReviewCancelled("Evidence search stopped by user.")
        self._wait_if_paused()


class MetricCard(QFrame):
    def __init__(self, label: str, value: str = "0", accent: str = Palette.blue):
        super().__init__()
        self.setObjectName("MetricCard")
        self.setFixedHeight(58)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        self.label = QLabel(label)
        self.label.setStyleSheet(f"color: {Palette.muted}; font-weight: 600;")
        self.value = QLabel(value)
        self.value.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        self.value.setStyleSheet(f"color: {accent};")
        layout.addWidget(self.label)
        layout.addStretch()
        layout.addWidget(self.value)

    def set_value(self, value: int | str) -> None:
        self.value.setText(str(value))


class RunProgressCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName('InlineRunProgress')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel('Kildeoppslag', self)
        self.detail = QLabel('Klar', self)
        self.count = QLabel('0 / 0 patients', self)
        self.title.hide()
        self.detail.hide()
        self.count.hide()
        self.bar = QProgressBar()
        self.bar.setObjectName('RunProgressBar')
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)
        self.hide()

    def update_progress(self, current: int, total: int, detail: str) -> None:
        safe_total = max(1, total)
        self.bar.setRange(0, safe_total)
        self.bar.setValue(min(max(0, current), safe_total))
        self.count.setText(f'{current} / {total} patients')
        self.detail.setText(detail)
        self.show()




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
        self.report_outcomes: dict[str, PatientReportOutcome] = {}
        self.processing_thread: QThread | None = None
        self.workbook_write_thread: QThread | None = None
        self.workbook_write_worker: WorkbookWriteWorker | None = None
        self._workbook_write_requested = False
        self._background_workbook_error: Exception | None = None
        self._background_workbook_path: Path | None = None
        self._workbook_write_started_at: float | None = None
        self.workbook_load_thread: QThread | None = None
        self.patient_report_thread: QThread | None = None
        self.patient_report_worker: PatientReportWorker | None = None
        self.report_retry_thread: QThread | None = None
        self.report_retry_worker: ReportRetryWorker | None = None
        self.database_thread: QThread | None = None
        self.browser_thread: QThread | None = None
        self.session_check_thread: QThread | None = None
        self.session_check_worker: BrowserSessionCheckWorker | None = None
        self._session_results: dict[str, object] = {}
        self.processing_worker: ProcessingWorker | None = None
        self.workbook_load_worker: ProcessedWorkbookWorker | None = None
        self.database_worker: DatabaseWorker | None = None
        self.browser_worker: BrowserLoginWorker | BrowserReviewWorker | None = None
        self._search_pause_requested = False
        self._search_stop_requested = False
        self._search_started_at: float | None = None
        self._operation_active = False
        self._active_sources: set[tuple[str, str]] = set()
        self._search_pending_pairs: set[tuple[str, str]] = set()
        self._queue_databases: set[str] = set()
        self._active_search_report_patient_ids: list[str] = []
        self._pending_report_after_workbook: list[str] = []
        self._waiting_final_workbook = False
        self.workbook_write_pending = False
        self._workbook_lock_warning_shown = False
        self.run_journal: RunJournal | None = None
        self._run_log_warning_shown = False
        self.setWindowTitle("VPM Tolkning")
        self.app_icon_path = (
            Path(__file__).resolve().parents[1] / "assets" / "vpm-tolkning-icon.png"
        )
        if self.app_icon_path.exists():
            self.setWindowIcon(QIcon(str(self.app_icon_path)))
        self.resize(1440, 900)
        self.setMinimumSize(920, 600)
        self._build_ui()
        self._update_evidence_summary()
        self._apply_style()
        for warning in self.settings.load_warnings:
            self._log(warning)

    @staticmethod
    def _thread_is_running(thread) -> bool:
        try:
            return thread is not None and thread.isRunning()
        except RuntimeError:
            return False

    def _worker_thread_finished(self) -> None:
        finished = self.sender()
        for thread_name, worker_name in (
            ("processing_thread", "processing_worker"),
            ("workbook_load_thread", "workbook_load_worker"),
            ("patient_report_thread", "patient_report_worker"),
            ("report_retry_thread", "report_retry_worker"),
            ("database_thread", "database_worker"),
            ("browser_thread", "browser_worker"),
            ("session_check_thread", "session_check_worker"),
        ):
            if getattr(self, thread_name) is finished:
                setattr(self, thread_name, None)
                setattr(self, worker_name, None)

    def closeEvent(self, event) -> None:
        for thread in (
            self.processing_thread, self.workbook_load_thread, self.workbook_write_thread,
            self.patient_report_thread, self.report_retry_thread, self.database_thread,
            self.browser_thread, self.session_check_thread,
        ):
            if self._thread_is_running(thread):
                event.ignore()
                self.status_bar.showMessage("Vent til arbeidet er ferdig før du lukker appen. Et aktivt søk kan avsluttes med Stop Search.")
                return
        super().closeEvent(event)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.navigation = NavigationRail(self.app_icon_path)
        self.navigation.page_requested.connect(self._switch_page)
        self.nav_group = self.navigation.group
        self.nav_buttons = self.navigation.buttons
        shell.addWidget(self.navigation)

        content = QWidget()
        content.setObjectName("ContentShell")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 12, 16, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        self.page_eyebrow = QLabel("VPM INTERPRETATION  /  IMPORT")
        self.page_eyebrow.setObjectName("PageEyebrow")
        self.page_title = QLabel("Importer analyse")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel(
            "Ny analyse eller fortsett fra en review-fil"
        )
        self.page_subtitle.setObjectName("PageSubtitle")
        self.page_subtitle.setWordWrap(True)
        self.page_eyebrow.hide()
        title_box.addWidget(self.page_title)
        title_box.addWidget(self.page_subtitle)
        header.addLayout(title_box, 1)
        self.activity_progress = QProgressBar()
        self.activity_progress.setObjectName("ActivityProgress")
        self.activity_progress.setRange(0, 0)
        self.activity_progress.setTextVisible(False)
        self.activity_progress.setFixedWidth(150)
        self.activity_progress.hide()
        header.addWidget(self.activity_progress)
        layout.addLayout(header)

        self.run_status_strip = RunStatusStrip()
        self.run_status_strip.pause_requested.connect(self._toggle_search_pause)
        self.run_status_strip.stop_requested.connect(self._stop_evidence_search)
        self.status_badge = self.run_status_strip.phase_label
        layout.addWidget(self.run_status_strip)

        self.run_progress = RunProgressCard()
        self.run_status_strip.layout().addWidget(self.run_progress)

        self.tabs = QStackedWidget()
        self.tabs.setObjectName("WorkspacePages")
        self.tabs.addWidget(self._processing_tab())
        self.tabs.addWidget(self._database_tab())
        self.tabs.addWidget(self._settings_tab_v2())
        layout.addWidget(self.tabs, 1)
        shell.addWidget(content, 1)

        for control in [
            *root.findChildren(QPushButton),
            *root.findChildren(QCheckBox),
        ]:
            control.setCursor(Qt.CursorShape.PointingHandCursor)
        for button in root.findChildren(QPushButton):
            if button.text() and button.objectName() != "SidebarButton":
                button.setMinimumHeight(max(44, button.minimumHeight()))

        self.setCentralWidget(root)
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _switch_page(self, index: int) -> None:
        pages = [
            ("IMPORT", "Importer analyse", "Ny analyse eller fortsett fra en review-fil"),
            ("EVIDENCE", "Kilder og søk", "Pasienter, kildetilgang og lagrede oppslag"),
            ("SETTINGS", "Innstillinger", "Referanselister, tilkoblinger og søkevalg"),
        ]
        if not 0 <= index < len(pages):
            return
        self.tabs.setCurrentIndex(index)
        self.navigation.set_current(index)
        eyebrow, title, subtitle = pages[index]
        self.page_eyebrow.setText(f"VPM INTERPRETATION  /  {eyebrow}")
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        self.page_subtitle.setVisible(index != 1)

    def _processing_tab(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.import_scroll = QScrollArea()
        self.import_scroll.setWidgetResizable(True)
        self.import_scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._import_ready_draft: tuple[Path | None, Path | None] | None = None
        self._import_excel_opened = False
        self.new_analysis_btn = QPushButton("Ny analyse · TSV")
        self.resume_analysis_btn = QPushButton("Fortsett analyse · Excel")
        self.import_mode_buttons = QButtonGroup(self)
        mode_row = QHBoxLayout()
        for index, button in enumerate((self.new_analysis_btn, self.resume_analysis_btn)):
            button.setCheckable(True)
            button.setObjectName("ImportModeButton")
            button.setMinimumHeight(44)
            self.import_mode_buttons.addButton(button, index)
            mode_row.addWidget(button, 1)
        self.new_analysis_btn.setChecked(True)
        layout.addLayout(mode_row)

        self.file_drop = AnalysisFileDrop()
        self.file_drop.file_selected.connect(self._open_analysis_file)
        layout.addWidget(self.file_drop)

        self.import_modes = QStackedWidget()
        new_page = QWidget()
        new_layout = QVBoxLayout(new_page)
        new_layout.setContentsMargins(0, 0, 0, 0)
        resume_page = QWidget()
        resume_page_layout = QVBoxLayout(resume_page)
        resume_page_layout.setContentsMargins(0, 0, 0, 0)
        resume_page_layout.setSpacing(8)
        self.import_modes.addWidget(new_page)
        self.import_modes.addWidget(resume_page)
        self.import_mode_buttons.idClicked.connect(self._set_import_mode)
        layout.addWidget(self.import_modes)

        files = QGroupBox("Analysefiler")
        grid = QGridLayout(files)
        grid.setContentsMargins(8, 24, 8, 8)
        grid.setVerticalSpacing(8)
        grid.setHorizontalSpacing(8)
        for row in range(3):
            grid.setRowMinimumHeight(row, 44)
        grid.setColumnStretch(1, 1)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Velg den filtrerte variantfilen (.tsv)")
        self.input_edit.setAccessibleName("Variantfil TSV")
        self.input_edit.textChanged.connect(self._update_process_state)
        self.input_edit.textChanged.connect(self._sync_run_date_from_paths)
        input_btn = QPushButton("Velg fil…")
        input_btn.clicked.connect(self._browse_input)
        self.input_browse_btn = input_btn
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Excel-fil for gjennomgang (.xlsx)")
        self.output_edit.setAccessibleName("Excel-fil for gjennomgang")
        self.output_edit.textChanged.connect(self._update_process_state)
        self.output_edit.textChanged.connect(self._sync_run_date_from_paths)
        output_btn = QPushButton("Velg mål…")
        output_btn.clicked.connect(self._browse_output)
        self.output_browse_btn = output_btn
        self.run_date = QDateEdit()
        self.run_date.setCalendarPopup(True)
        self.run_date.setDisplayFormat("yyyy-MM-dd")
        self.run_date.setDate(QDate.currentDate())
        for control in (self.input_edit, self.output_edit, self.run_date, input_btn, output_btn):
            control.setMinimumHeight(44)
        self.hide_excluded = QCheckBox("Skjul ekskluderte rader")
        self.hide_excluded.setChecked(False)
        grid.addWidget(QLabel("Variantfil"), 0, 0)
        grid.addWidget(self.input_edit, 0, 1)
        grid.addWidget(input_btn, 0, 2)
        grid.addWidget(QLabel("Excel-fil"), 1, 0)
        grid.addWidget(self.output_edit, 1, 1)
        grid.addWidget(output_btn, 1, 2)
        grid.addWidget(QLabel("Sekvenseringsdato"), 2, 0)
        grid.addWidget(self.run_date, 2, 1)
        grid.addWidget(self.hide_excluded, 2, 2)
        new_layout.addWidget(files)

        actions = QHBoxLayout()
        self.validate_btn = QPushButton("Valider TSV")
        self.validate_btn.clicked.connect(self._validate_input)
        self.process_btn = QPushButton("Opprett review-fil")
        self.process_btn.setObjectName("PrimaryButton")
        self.process_btn.setEnabled(False)
        self.process_btn.clicked.connect(self._start_processing)
        self.open_workbook_btn = QPushButton("Åpne i Excel")
        self.open_workbook_btn.setObjectName("OutlineButton")
        self.open_workbook_btn.setEnabled(False)
        self.open_workbook_btn.clicked.connect(self._open_review_workbook)
        self.continue_evidence_btn = QPushButton("Til kilder og søk")
        self.continue_evidence_btn.setEnabled(False)
        self.continue_evidence_btn.clicked.connect(lambda: self._switch_page(1))
        actions.addStretch()
        actions.addWidget(self.validate_btn)
        actions.addWidget(self.open_workbook_btn)
        actions.addWidget(self.process_btn)
        review_guidance = QHBoxLayout()
        self.import_guidance = QLabel()
        self.import_guidance.setObjectName("HelperText")
        self.import_guidance.setWordWrap(True)
        review_guidance.addWidget(self.import_guidance, 1)
        review_guidance.addWidget(self.continue_evidence_btn)

        self.recent_analysis_panel = QFrame()
        self.recent_analysis_panel.setObjectName("RecentAnalysisPanel")
        recent_layout = QHBoxLayout(self.recent_analysis_panel)
        recent_layout.setContentsMargins(14, 12, 14, 12)
        recent_copy = QVBoxLayout()
        recent_title = QLabel("Nylig analyse")
        recent_title.setObjectName("SectionTitle")
        self.recent_analysis_name = QLabel()
        self.recent_analysis_name.setObjectName("FieldLabel")
        self.recent_analysis_detail = QLabel()
        self.recent_analysis_detail.setObjectName("HelperText")
        self.recent_analysis_name.setWordWrap(True)
        self.recent_analysis_detail.setWordWrap(True)
        recent_copy.addWidget(recent_title)
        recent_copy.addWidget(self.recent_analysis_name)
        recent_copy.addWidget(self.recent_analysis_detail)
        recent_layout.addLayout(recent_copy, 1)
        self.restore_recent_button = QPushButton("Gjenåpne")
        self.restore_recent_button.setObjectName("PrimaryButton")
        self.restore_recent_button.setMinimumHeight(44)
        self.dismiss_recent_button = QPushButton("Skjul")
        self.dismiss_recent_button.setMinimumHeight(44)
        self.dismiss_recent_button.clicked.connect(self.recent_analysis_panel.hide)
        recent_layout.addWidget(self.dismiss_recent_button)
        recent_layout.addWidget(self.restore_recent_button)
        self.recent_analysis_panel.hide()
        if self.settings.offer_recent_analysis and self.settings.last_processed_workbook:
            recent = inspect_recent_analysis(self.settings.last_processed_workbook)
            if recent.valid:
                self.recent_analysis_name.setText(recent.path.name)
                modified = (
                    recent.modified_at.strftime("%Y-%m-%d %H:%M")
                    if recent.modified_at
                    else "Unknown time"
                )
                self.recent_analysis_detail.setText(f"{modified} · {recent.message}")
                self.restore_recent_button.clicked.connect(
                    lambda checked=False, path=recent.path: self._load_processed_workbook(path)
                )
                self.recent_analysis_panel.show()
        resume_page_layout.addWidget(self.recent_analysis_panel)

        resume = QGroupBox("Fortsett fra Excel")
        resume_layout = QVBoxLayout(resume)
        resume_layout.setSpacing(8)
        resume_help = QLabel(
            "Gjenåpne en VPM review-fil med varianter, X-valg og tidligere oppslagsresultater."
        )
        resume_help.setObjectName("HelperText")
        resume_help.setWordWrap(True)
        resume_row = QHBoxLayout()
        self.resume_edit = QLineEdit()
        self.resume_edit.setReadOnly(True)
        self.resume_edit.setPlaceholderText("Ingen analyse gjenåpnet")
        self.resume_edit.setAccessibleName("Tidligere VPM review-fil")
        self.resume_btn = QPushButton("Velg Excel-fil…")
        self.resume_btn.setObjectName("OutlineButton")
        self.resume_btn.setMinimumHeight(44)
        self.resume_btn.setToolTip(
            "Gjenåpne en review-fil fra VPM Tolkning og fortsett uferdige oppslag."
        )
        self.resume_btn.clicked.connect(self._browse_processed_workbook)
        resume_row.addWidget(self.resume_edit, 1)
        resume_row.addWidget(self.resume_btn)
        self.resume_status = QLabel("Velg filen for å fortsette en tidligere analyse.")
        self.resume_status.setObjectName("HelperText")
        self.resume_status.setWordWrap(True)
        self.resume_edit.setMinimumHeight(44)
        resume_layout.addWidget(resume_help)
        resume_layout.addLayout(resume_row)
        resume_layout.addWidget(self.resume_status)
        resume_page_layout.addWidget(resume)
        resume_page_layout.addStretch()
        layout.addLayout(actions)
        layout.addLayout(review_guidance)

        activity = QGroupBox("Aktivitetslogg")
        activity.setMinimumHeight(190)
        activity_layout = QVBoxLayout(activity)
        activity_help = QLabel("Meldinger fra validering og kjøring.")
        activity_help.setObjectName("HelperText")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(120)
        self.log.setMaximumBlockCount(500)
        self.log.setPlaceholderText("Ingen aktivitet ennå. Velg en analysefil for å begynne.")
        log_actions = QHBoxLayout()
        log_actions.addStretch()
        self.open_log_folder_btn = QPushButton("Åpne loggmappe")
        self.open_log_folder_btn.setMinimumHeight(44)
        self.open_log_folder_btn.setEnabled(False)
        self.open_log_folder_btn.clicked.connect(self._open_run_log_folder)
        log_actions.addWidget(self.open_log_folder_btn)
        activity_layout.addWidget(activity_help)
        activity_layout.addWidget(self.log, 1)
        activity_layout.addLayout(log_actions)
        layout.addWidget(activity)
        layout.addStretch()
        self.import_scroll.setWidget(content)
        page_layout.addWidget(self.import_scroll)
        self._refresh_import_step()
        return page

    def _set_import_mode(self, index: int) -> None:
        self.import_modes.setCurrentIndex(index)
        self.import_mode_buttons.button(index).setChecked(True)
        self.import_scroll.verticalScrollBar().setValue(0)
        self._refresh_import_step()

    def _import_draft(self) -> tuple[Path | None, Path | None]:
        paths = []
        for index, field in enumerate((self.input_edit, self.output_edit)):
            text = field.text().strip()
            path = Path(text) if text else None
            if path is not None:
                try:
                    if index == 1 and path.suffix.lower() != '.xlsx':
                        path = path.with_suffix('.xlsx')
                    path = path.resolve()
                except (OSError, ValueError):
                    # Invalid typed paths remain drafts and are validated on use.
                    pass
            paths.append(path)
        return paths[0], paths[1]

    def _refresh_import_step(self) -> None:
        if not hasattr(self, "continue_evidence_btn"):
            return
        ready = self.result is not None and self._import_ready_draft == self._import_draft()
        new_mode = self.import_modes.currentIndex() == 0
        available = not self._operation_active
        self.validate_btn.setVisible(new_mode and not ready)
        self.process_btn.setVisible(new_mode and not ready)
        self.process_btn.setEnabled(
            new_mode and not ready and available
            and Path(self.input_edit.text()).is_file() and bool(self.output_edit.text().strip())
        )
        self.open_workbook_btn.setVisible(ready)
        self.continue_evidence_btn.setVisible(ready)
        self.open_workbook_btn.setEnabled(ready and available)
        self.continue_evidence_btn.setEnabled(ready and available)
        next_button = self.continue_evidence_btn if self._import_excel_opened else self.open_workbook_btn
        for button in (self.open_workbook_btn, self.continue_evidence_btn):
            button.setObjectName("PrimaryButton" if ready and button is next_button else "OutlineButton")
            button.style().unpolish(button)
            button.style().polish(button)
        self.resume_btn.setObjectName("PrimaryButton" if not ready and not new_mode else "OutlineButton")
        self.resume_btn.style().unpolish(self.resume_btn)
        self.resume_btn.style().polish(self.resume_btn)
        if ready and self._import_excel_opened:
            guidance = "Lagre filen i Excel. Last inn X-valgene på «Kilder og søk» før du søker."
        elif ready:
            guidance = "Åpne review-filen i Excel og marker X ved oppslag som skal hoppes over."
        elif new_mode:
            guidance = "Opprett review-filen, gjennomgå den i Excel og marker X for oppslag som skal hoppes over."
        else:
            guidance = "Velg en tidligere review-fil for å fortsette med lagrede X-valg og oppslagsresultater."
        self.import_guidance.setText(guidance)

    def _database_tab(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.database_scroll = QScrollArea()
        self.database_scroll.setWidgetResizable(True)
        self.database_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.database_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        file_row = QHBoxLayout()
        self.current_workbook_label = QLabel('Ingen review-fil lastet')
        self.current_workbook_label.setObjectName('FieldLabel')
        self.current_workbook_label.setTextFormat(Qt.TextFormat.PlainText)
        self.current_workbook_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.check_sessions_btn = QPushButton('Sjekk innlogging')
        self.check_sessions_btn.setObjectName('OutlineButton')
        self.check_sessions_btn.setToolTip('Kontroller alle kilder i Edge uten å sende varianter.')
        self.check_sessions_btn.clicked.connect(self._start_session_check)
        file_row.addWidget(self.current_workbook_label, 1)
        file_row.addWidget(self.check_sessions_btn)
        layout.addLayout(file_row)
        self.session_check_summary = QLabel('Innlogging er ikke sjekket i denne økten.')
        self.session_check_summary.setObjectName('HelperText')
        self.session_check_summary.setWordWrap(True)
        self.session_check_summary.hide()

        sources = QHBoxLayout()
        sources.setSpacing(8)
        self.db_checks = {}
        self.session_status_labels = {}
        self.source_access_buttons = {}
        for database in self.databases:
            tile = QFrame()
            tile.setObjectName('SourceTile')
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(8, 0, 8, 4)
            tile_layout.setSpacing(0)
            check = QCheckBox(database)
            check.setMinimumHeight(36)
            check.setChecked(database in self.settings.enabled_databases)
            check.setToolTip('Ta med denne kilden i søket. Innlogging bruker appens Edge-profil.')
            check.stateChanged.connect(self._update_evidence_summary)
            self.db_checks[database] = check
            tile_layout.addWidget(check)
            access_row = QHBoxLayout()
            access_row.setSpacing(4)
            status = QLabel('Ikke sjekket')
            status.setObjectName('HelperText')
            status.setTextFormat(Qt.TextFormat.PlainText)
            status.setWordWrap(True)
            status.setAccessibleName(f'{database}: tilgangsstatus')
            status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.session_status_labels[database] = status
            access = QToolButton()
            access.setText('Sjekk')
            access.setAccessibleName(f'Sjekk tilgang til {database}')
            access.setCursor(Qt.CursorShape.PointingHandCursor)
            access.clicked.connect(lambda checked=False, db=database: self._source_access_action(db))
            self.source_access_buttons[database] = access
            access_row.addWidget(status, 1)
            access_row.addWidget(access)
            tile_layout.addLayout(access_row)
            sources.addWidget(tile, 1)
        layout.addLayout(sources)

        scope = QHBoxLayout()
        self.load_selection_btn = QPushButton('Hent X-valg fra Excel')
        self.load_selection_btn.setEnabled(False)
        self.load_selection_btn.setToolTip('Lagre og lukk review-filen i Excel først. Velg filen for å hente X-markeringene.')
        self.load_selection_btn.clicked.connect(self._load_database_selection)
        self.selection_status = QLabel('X-valg er ikke hentet i denne økten.')
        self.selection_status.setObjectName('HelperText')
        self.selection_status.setWordWrap(True)
        self.selection_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.included_only_check = QCheckBox('Bare inkluderte')
        self.included_only_check.setChecked(self.settings.search_included_only)
        self.included_only_check.setToolTip('Ekskluderte og flaggede varianter sendes ikke når dette er valgt.')
        self.included_only_check.stateChanged.connect(self._update_evidence_summary)
        scope.addWidget(self.load_selection_btn)
        scope.addWidget(self.selection_status, 1)
        scope.addWidget(self.included_only_check)
        layout.addLayout(scope)

        self.evidence_summary = QLabel('Opprett eller åpne en review-fil for å søke.')
        self.evidence_summary.setObjectName('HelperText')
        self.evidence_summary.setWordWrap(True)
        self.evidence_summary.hide()
        commands = QHBoxLayout()
        self.search_btn = QPushButton('Søk ventende varianter')
        self.search_btn.setObjectName('PrimaryButton')
        self.search_btn.setEnabled(False)
        self.search_btn.clicked.connect(self._start_primary_search)
        self.pause_search_btn = self.run_status_strip.pause_button
        self.stop_search_btn = self.run_status_strip.stop_button
        self.priority_selection_status = QLabel('Ingen pasienter avkrysset · søk gjelder alle')
        self.priority_selection_status.setObjectName('HelperText')
        self.priority_selection_status.setWordWrap(True)
        self.priority_selection_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.select_all_patients_check = QCheckBox('Velg alle')
        self.select_all_patients_check.setAccessibleName('Kryss av alle pasienter')
        self.select_all_patients_check.toggled.connect(self._select_all_patients)
        self.patient_details_btn = QPushButton('Detaljer')
        self.patient_details_btn.clicked.connect(self._show_patient_details)
        commands.addWidget(self.search_btn)
        commands.addWidget(self.priority_selection_status, 1)
        commands.addWidget(self.select_all_patients_check)
        commands.addWidget(self.patient_details_btn)
        layout.addLayout(commands)
        self.status_matrix = StatusMatrix(self.databases)
        self.status_matrix.setMinimumHeight(150)
        self.status_matrix.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.status_matrix.patient_selection_changed.connect(self._update_priority_controls)
        self.status_matrix.patient_focused.connect(self._update_patient_focus)
        self.status_matrix.cell_activated.connect(lambda patient, column: self._show_patient_details(patient))
        layout.addWidget(self.status_matrix, 1)

        exports = QFrame()
        exports.setObjectName('ReportsGroup')
        export_layout = QHBoxLayout(exports)
        export_layout.setContentsMargins(8, 4, 8, 6)
        self.patient_excel_btn = QPushButton('Generer vedlegg')
        self.patient_excel_btn.setEnabled(False)
        self.patient_excel_btn.setToolTip('Lager VEDLEGG_APP for omfanget som står på knappen.')
        self.patient_excel_btn.clicked.connect(self._export_patient_excels)
        self.rewrite_btn = QPushButton('Lagre resultater i Excel')
        self.rewrite_btn.setEnabled(False)
        self.rewrite_btn.clicked.connect(self._rewrite_workbook)
        self.advanced_evidence_btn = QPushButton('Avansert')
        self.advanced_evidence_btn.setCheckable(True)
        self.advanced_evidence_btn.toggled.connect(self._toggle_evidence_advanced)
        export_layout.addWidget(self.patient_excel_btn)
        export_layout.addWidget(self.rewrite_btn)
        export_layout.addStretch()
        export_layout.addWidget(self.advanced_evidence_btn)
        self.latest_action_label = QLabel('Klar til søk')
        self.latest_action_label.setObjectName('HelperText')
        self.latest_action_label.setTextFormat(Qt.TextFormat.PlainText)
        self.latest_action_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.latest_action_label.hide()

        self.advanced_evidence_content = QGroupBox('Feil og nettleser')
        advanced = QGridLayout(self.advanced_evidence_content)
        self.retry_selected_btn = QPushButton('Prøv feilede på nytt')
        self.retry_selected_btn.setEnabled(False)
        self.retry_selected_btn.clicked.connect(self._start_selected_retry)
        self.priority_search_btn = QPushButton('Søk valgte pasienter')
        self.priority_search_btn.setEnabled(False)
        self.priority_search_btn.clicked.connect(self._start_prioritized_search)
        self.remaining_search_btn = QPushButton('Søk alle ventende')
        self.remaining_search_btn.setEnabled(False)
        self.remaining_search_btn.clicked.connect(self._start_remaining_search)
        self.browser_database_combo = QComboBox()
        self.browser_database_combo.addItems(list(BROWSER_DATABASES))
        self.browser_signin_btn = QPushButton('Logg inn')
        self.browser_signin_btn.clicked.connect(lambda: self._start_browser_login())
        self.browser_review_btn = QPushButton('Kjør nettleserkilder')
        self.browser_review_btn.setEnabled(False)
        self.browser_review_btn.clicked.connect(self._start_browser_review)
        self.retry_report_saves_button = QPushButton('Prøv ventende lagring igjen')
        self.retry_report_saves_button.clicked.connect(self._retry_pending_report_saves)
        self.retry_report_saves_button.hide()
        advanced.addWidget(self.retry_selected_btn, 0, 0)
        advanced.addWidget(self.remaining_search_btn, 0, 1)
        advanced.addWidget(self.priority_search_btn, 0, 2)
        advanced.addWidget(self.browser_database_combo, 1, 0)
        advanced.addWidget(self.browser_signin_btn, 1, 1)
        advanced.addWidget(self.browser_review_btn, 1, 2)
        advanced.addWidget(self.session_check_summary, 2, 0, 1, 3)
        self.session_check_summary.show()
        advanced.addWidget(self.retry_report_saves_button, 3, 0, 1, 3)
        self.advanced_evidence_content.hide()
        layout.addWidget(self.advanced_evidence_content)
        self.database_scroll.setWidget(content)
        page_layout.addWidget(self.database_scroll, 1)
        page_layout.addWidget(exports)
        return page

    def _start_primary_search(self) -> None:
        if self._explicitly_selected_patient_ids():
            self._start_prioritized_search()
        else:
            self._start_remaining_search()

    def _select_all_patients(self, checked: bool) -> None:
        if checked:
            self.status_matrix.select_all_patients()
        else:
            self.status_matrix.unselect_all_patients()

    def _toggle_evidence_advanced(self, expanded: bool) -> None:
        self.advanced_evidence_content.setVisible(expanded)
        self.advanced_evidence_btn.setText('Skjul avansert' if expanded else 'Avansert')

    def _source_access_action(self, database: str) -> None:
        result = self._session_results.get(database)
        if result and result.status == 'login_required':
            self._start_browser_login(database)
        else:
            self._check_browser_sessions(database)

    def _update_source_access_controls(self) -> None:
        for database, button in getattr(self, 'source_access_buttons', {}).items():
            result = self._session_results.get(database)
            login = result is not None and result.status == 'login_required'
            button.setText('Logg inn' if login else 'Sjekk')
            button.setAccessibleName(f"{'Logg inn på' if login else 'Sjekk tilgang til'} {database}")
            button.setEnabled(not self._operation_active)

    def _update_patient_focus(self, patient_id: str) -> None:
        self.patient_details_btn.setEnabled(bool(patient_id))

    def _show_patient_details(self, patient_id: str | bool | None = None) -> None:
        patient = patient_id if isinstance(patient_id, str) else self.status_matrix.focused_patient()
        row = self.status_matrix.patient_details(patient)
        if row is None:
            return
        dialog = PatientDetailsDialog(row, self)

        def refresh(*_args) -> None:
            sources = [db for db, check in self.db_checks.items() if check.isChecked()]
            retry_enabled = not self._operation_active and bool(_retryable_source_pairs(
                self._variants_for_search(patient_ids={patient}), self.evidence, sources,
            ))
            dialog.set_patient(self.status_matrix.patient_details(patient), retry_enabled=retry_enabled)

        dialog.retry_requested.connect(lambda selected: self._start_database_search(
            patient_ids={selected}, retry_failed_only=True,
        ))
        self.status_matrix.patient_details_changed.connect(refresh)
        refresh()
        try:
            dialog.exec()
        finally:
            self.status_matrix.patient_details_changed.disconnect(refresh)
            dialog.deleteLater()


    def _settings_tab_v2(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        settings_content = QWidget()
        layout = QVBoxLayout(settings_content)
        layout.setSpacing(12)

        local_group = QGroupBox("Rapportmappe og referanselister")
        local_grid = QGridLayout(local_group)
        local_grid.setColumnStretch(1, 1)
        self.output_dir_edit = QLineEdit(self.settings.default_output_dir)
        dir_btn = QPushButton("Velg mappe")
        dir_btn.clicked.connect(self._browse_output_dir)
        local_grid.addWidget(QLabel("Rapportmappe"), 0, 0)
        local_grid.addWidget(self.output_dir_edit, 0, 1)
        local_grid.addWidget(dir_btn, 0, 2)
        self.who_genes_edit = QLineEdit(self.settings.who_driver_genes_path)
        self.who_genes_edit.setPlaceholderText("Innebygd WHO-drivergenliste brukes når feltet er tomt")
        self.who_genes_edit.setAccessibleName("WHO-drivergenfil")
        self.who_genes_edit.editingFinished.connect(self._validate_who_path)
        who_btn = QPushButton("Velg fil")
        who_btn.clicked.connect(self._browse_who_genes)
        local_grid.addWidget(QLabel("WHO-drivergener"), 1, 0)
        local_grid.addWidget(self.who_genes_edit, 1, 1)
        local_grid.addWidget(who_btn, 1, 2)
        self.who_path_status = QLabel()
        self.who_path_status.setObjectName("HelperText")
        self.who_path_status.setWordWrap(True)
        local_grid.addWidget(self.who_path_status, 2, 1, 1, 2)
        self._validate_who_path()
        if self.settings.load_warnings:
            settings_warning = QLabel("\n".join(self.settings.load_warnings))
            settings_warning.setWordWrap(True)
            settings_warning.setStyleSheet(f"color: {Palette.red};")
            local_grid.addWidget(settings_warning, 5, 0, 1, 3)
        self.artifact_path_edit = QLineEdit(self.settings.artifact_rules_path)
        self.artifact_path_edit.setPlaceholderText("Lagrede artefaktregler brukes når feltet er tomt")
        self.artifact_path_edit.setAccessibleName("Artefaktliste i Excel")
        self.artifact_path_edit.editingFinished.connect(self._validate_artifact_path)
        artifact_file_btn = QPushButton("Velg fil")
        artifact_file_btn.clicked.connect(self._browse_artifact_catalog)
        local_grid.addWidget(QLabel("Artefaktliste"), 3, 0)
        local_grid.addWidget(self.artifact_path_edit, 3, 1)
        local_grid.addWidget(artifact_file_btn, 3, 2)
        self.artifact_path_status = QLabel()
        self.artifact_path_status.setObjectName("HelperText")
        self.artifact_path_status.setWordWrap(True)
        local_grid.addWidget(self.artifact_path_status, 4, 1, 1, 2)
        self.reference_selection_status = QLabel()
        self.reference_selection_status.setObjectName('HelperText')
        self.reference_selection_status.setWordWrap(True)
        reference_note = QLabel('Excel-lister leses ved neste behandling eller gjenåpning. Endringer brukes ikke midt i et aktivt søk.')
        reference_note.setObjectName('HelperText')
        reference_note.setWordWrap(True)
        local_grid.addWidget(self.reference_selection_status, 6, 0, 1, 3)
        local_grid.addWidget(reference_note, 7, 0, 1, 3)
        for edit in (self.output_dir_edit, self.who_genes_edit, self.artifact_path_edit):
            edit.textChanged.connect(self._update_reference_selection_status)
        reference_actions = QHBoxLayout()
        self.open_who_reference_btn = QPushButton('Åpne WHO-listen')
        self.open_who_reference_btn.clicked.connect(lambda: self._open_reference_file(self.who_genes_edit))
        self.open_artifact_reference_btn = QPushButton('Åpne artefaktlisten')
        self.open_artifact_reference_btn.clicked.connect(lambda: self._open_reference_file(self.artifact_path_edit))
        reference_actions.addWidget(self.open_who_reference_btn)
        reference_actions.addWidget(self.open_artifact_reference_btn)
        local_grid.addLayout(reference_actions, 8, 0, 1, 3)

        access_group = QGroupBox("Tilgang i Edge")
        access_grid = QGridLayout(access_group)
        for column in range(1, 4):
            access_grid.setColumnStretch(column, 1)
        for column, title in enumerate(["Kilde", "Tilgang", "E-post", "Passord"]):
            header = QLabel(title)
            header.setObjectName("SettingsColumnHeader")
            access_grid.addWidget(header, 0, column)

        self.cosmic_email_edit = QLineEdit(self.settings.cosmic_email)
        self.cosmic_password_edit = QLineEdit(self.settings.cosmic_password)
        self.cosmic_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.oncokb_email_edit = QLineEdit(self.settings.oncokb_email)
        self.oncokb_password_edit = QLineEdit(self.settings.oncokb_password)
        self.oncokb_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.franklin_email_edit = QLineEdit(self.settings.franklin_email)
        self.franklin_password_edit = QLineEdit(self.settings.franklin_password)
        self.franklin_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.mtbp_email_edit = QLineEdit(self.settings.mtbp_email)
        self.mtbp_password_edit = QLineEdit(self.settings.mtbp_password)
        self.mtbp_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        def not_required() -> QLabel:
            label = QLabel("Ikke nødvendig")
            label.setObjectName("NotRequired")
            return label

        def web_access(text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("WebAccess")
            return label

        provider_rows = [
            ("ClinVar", web_access("Offentlig nettsted"), not_required(), not_required()),
            ("COSMIC", web_access("Innlogget nettsted"), self.cosmic_email_edit, self.cosmic_password_edit),
            ("OncoKB", web_access("Innlogget nettsted"), self.oncokb_email_edit, self.oncokb_password_edit),
            ("Franklin", web_access("Innlogget nettsted"), self.franklin_email_edit, self.franklin_password_edit),
            ("MTBP", web_access("Innlogget nettsted"), self.mtbp_email_edit, self.mtbp_password_edit),
        ]
        for row, (provider, access, email, password) in enumerate(provider_rows, start=1):
            provider_label = QLabel(provider)
            provider_label.setObjectName("FieldLabel")
            access_grid.addWidget(provider_label, row, 0)
            access_grid.addWidget(access, row, 1)
            access_grid.addWidget(email, row, 2)
            access_grid.addWidget(password, row, 3)


        safety_group = QGroupBox("Søkevalg")
        safety_grid = QGridLayout(safety_group)
        safety_grid.setColumnStretch(1, 1)
        self.mtbp_cancer_type_edit = QLineEdit(self.settings.mtbp_cancer_type)
        self.mtbp_cancer_type_edit.setPlaceholderText("Exact MTBP cancer type, for example Blood")
        self.browser_delay_spin = QSpinBox()
        self.browser_delay_spin.setRange(0, 120)
        self.browser_delay_spin.setSuffix(" s")
        self.browser_delay_spin.setValue(self.settings.browser_delay_seconds)
        self.browser_delay_spin.setToolTip(
            "Minimum randomized pause between variants on the same website."
        )
        self.browser_delay_max_spin = QSpinBox()
        self.browser_delay_max_spin.setRange(0, 120)
        self.browser_delay_max_spin.setSuffix(" s")
        self.browser_delay_max_spin.setValue(self.settings.browser_delay_max_seconds)
        self.browser_delay_max_spin.setToolTip(
            "Maximum randomized pause between variants on the same website."
        )
        delay_layout = QHBoxLayout()
        delay_layout.setContentsMargins(0, 0, 0, 0)
        delay_layout.addWidget(QLabel("Minimum"))
        delay_layout.addWidget(self.browser_delay_spin)
        delay_layout.addSpacing(12)
        delay_layout.addWidget(QLabel("Maksimum"))
        delay_layout.addWidget(self.browser_delay_max_spin)
        provider_switch_note = QLabel(
            "Fast pause på 3 sekunder mellom kilder."
        )
        provider_switch_note.setObjectName("HelperText")
        provider_switch_note.setWordWrap(True)
        delay_layout.addStretch()
        self.mtbp_timeout_spin = QSpinBox()
        self.mtbp_timeout_spin.setRange(5, 60)
        self.mtbp_timeout_spin.setSuffix(" min")
        self.mtbp_timeout_spin.setValue(self.settings.mtbp_timeout_minutes)
        self.browser_background_check = QCheckBox(
            "Hold automatiserte Edge-vinduer minimert"
        )
        self.browser_background_check.setChecked(self.settings.browser_background)
        self.browser_background_check.stateChanged.connect(
            self._update_evidence_summary
        )
        self.browser_background_check.setToolTip(
            "Recommended while you work in other programs. Sign-in windows still open visibly when requested."
        )
        safety_grid.addWidget(QLabel("MTBP krefttype"), 0, 0)
        safety_grid.addWidget(self.mtbp_cancer_type_edit, 0, 1)
        safety_grid.addWidget(QLabel("Mellom varianter"), 1, 0)
        safety_grid.addLayout(delay_layout, 1, 1)
        safety_grid.addWidget(QLabel("Mellom kilder"), 2, 0)
        safety_grid.addWidget(provider_switch_note, 2, 1)
        safety_grid.addWidget(QLabel("MTBP tidsgrense"), 3, 0)
        safety_grid.addWidget(self.mtbp_timeout_spin, 3, 1)
        safety_grid.addWidget(QLabel("Edge-vinduer"), 4, 0)
        safety_grid.addWidget(self.browser_background_check, 4, 1)


        artifact_group = QGroupBox("Manuelle artefaktregler")
        artifact_layout = QVBoxLayout(artifact_group)
        artifact_note = QLabel(
            f"Innebygd liste: {len(default_artifact_rules())} regler, inkludert Artefakter v7. "
            "Reglene under brukes når ingen Excel-liste er valgt. "
            "En valgt Excel-liste erstatter hele listen ved neste behandling eller gjenåpning. "
            "ASXL1 NM_015338.5:c.1934dup beholdes når AF er over 5.5%."
        )
        artifact_note.setObjectName("HelperText")
        artifact_note.setWordWrap(True)
        artifact_layout.addWidget(artifact_note)
        self.artifact_table = QTableWidget(0, 4)
        self.artifact_table.setHorizontalHeaderLabels(
            ["Gen", "HGVSc", "Artefakt til og med AF", "Begrunnelse"]
        )
        artifact_header = self.artifact_table.horizontalHeader()
        artifact_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        artifact_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        artifact_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        artifact_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.artifact_table.setMinimumHeight(240)
        self._load_artifact_table(self.settings.artifact_rules)
        artifact_actions = QHBoxLayout()
        add_artifact_btn = QPushButton("Legg til regel")
        add_artifact_btn.clicked.connect(self._add_artifact_row)
        remove_artifact_btn = QPushButton("Fjern valgt")
        remove_artifact_btn.clicked.connect(self._remove_selected_artifact)
        reset_artifact_btn = QPushButton("Tilbakestill regler")
        reset_artifact_btn.clicked.connect(self._reset_default_artifacts)
        self.artifact_edit_buttons = [add_artifact_btn, remove_artifact_btn, reset_artifact_btn]
        artifact_actions.addWidget(add_artifact_btn)
        artifact_actions.addWidget(remove_artifact_btn)
        artifact_actions.addWidget(reset_artifact_btn)
        artifact_actions.addStretch()
        artifact_layout.addWidget(self.artifact_table)
        artifact_layout.addLayout(artifact_actions)
        self.artifact_path_edit.textChanged.connect(self._artifact_source_changed)
        self._validate_artifact_path()

        self.settings_groups = [
            local_group,
            access_group,
            safety_group,
            artifact_group,
        ]

        layout.addWidget(local_group)
        layout.addStretch()
        self.settings_scroll.setWidget(settings_content)
        self.settings_sections = QTabWidget()
        self.settings_sections.addTab(self.settings_scroll, 'Referanselister')
        for title, groups in [('Tilkoblinger', [access_group]), ('Avansert', [safety_group, artifact_group])]:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            section = QWidget()
            section_layout = QVBoxLayout(section)
            for group in groups:
                section_layout.addWidget(group)
            section_layout.addStretch()
            scroll.setWidget(section)
            self.settings_sections.addTab(scroll, title)
        page_layout.addWidget(self.settings_sections, 1)
        save_row = QHBoxLayout()
        self.save_settings_btn = QPushButton('Lagre innstillinger')
        self.save_settings_btn.setObjectName('PrimaryButton')
        self.save_settings_btn.clicked.connect(lambda: self._save_settings())
        save_row.addStretch()
        save_row.addWidget(self.save_settings_btn)
        page_layout.addLayout(save_row)
        self._update_reference_selection_status()
        return page

    def _update_reference_selection_status(self) -> None:
        if not hasattr(self, 'reference_selection_status'):
            return
        edits = (self.output_dir_edit, self.who_genes_edit, self.artifact_path_edit)
        saved = (self.settings.default_output_dir, self.settings.who_driver_genes_path, self.settings.artifact_rules_path)
        changed = any(edit.text().strip() != value for edit, value in zip(edits, saved))
        self.reference_selection_status.setText('Ikke lagret · lagre innstillinger for å bruke dette filvalget.' if changed else 'Filvalg er lagret.')
        self.reference_selection_status.setStyleSheet(f'color: {Palette.yellow if changed else Palette.muted};')
        if hasattr(self, 'open_who_reference_btn'):
            self.open_who_reference_btn.setEnabled(bool(self.who_genes_edit.text().strip()))
            self.open_artifact_reference_btn.setEnabled(bool(self.artifact_path_edit.text().strip()))

    def _open_reference_file(self, field: QLineEdit) -> None:
        path = Path(field.text().strip()).expanduser()
        if not field.text().strip() or not path.is_file():
            QMessageBox.warning(self, 'Referansefil mangler', 'Velg en eksisterende Excel-fil først.')
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))):
            QMessageBox.warning(self, 'Kunne ikke åpne filen', str(path))



    def _browse_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select variant TSV", "", "TSV files (*.tsv *.txt);;All files (*.*)")
        if path:
            self._open_analysis_file(path)

    def _open_analysis_file(self, filename: str) -> None:
        if self._operation_active:
            return
        path = Path(filename)
        if not path.is_file():
            return
        if path.suffix.casefold() == ".xlsx":
            self._load_processed_workbook(path)
        elif path.suffix.casefold() in {".tsv", ".txt"}:
            self._set_import_mode(0)
            self.input_edit.setText(str(path))
            output = Path(self.settings.default_output_dir) / f"{path.stem}_VPM_review.xlsx"
            self.output_edit.setText(str(output))
            self._refresh_import_step()
            self._switch_page(0)

    def _sync_run_date_from_paths(self) -> None:
        run_date = (
            sequencing_date_from_path(Path(self.input_edit.text()))
            or sequencing_date_from_path(Path(self.output_edit.text()))
        )
        if run_date:
            self.run_date.setDate(QDate.fromString(run_date, "yyyy-MM-dd"))
            self.run_date.setToolTip("Hentet fra navnet på VPM-mappen")
        else:
            self.run_date.setDate(QDate.currentDate())
            self.run_date.setToolTip("Ingen datert VPM-mappe funnet; velg dato manuelt")
        self.run_date.setEnabled(run_date is None)

    def _browse_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Workbook", "", "Excel workbook (*.xlsx)")
        if path:
            self.output_edit.setText(path if path.lower().endswith(".xlsx") else f"{path}.xlsx")

    def _open_review_workbook(self) -> None:
        if self._operation_active:
            return
        path = self.result.output_path if self.result else None
        if path is None or not Path(path).is_file():
            QMessageBox.warning(self, "Excel-fil mangler", "Opprett eller åpne en review-fil først.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve()))):
            QMessageBox.warning(self, "Kunne ikke åpne fil", f"Åpne filen manuelt:\n{path}")
            return
        self._import_excel_opened = True
        self._refresh_import_step()

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Default Output Folder", self.output_dir_edit.text())
        if path:
            self.output_dir_edit.setText(path)

    def _browse_who_genes(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Velg WHO-drivergenfil", self.who_genes_edit.text(),
            "Genlister (*.xlsx *.csv *.txt)",
        )
        if path:
            self.who_genes_edit.setText(path)
            self._validate_who_path()

    def _validate_who_path(self) -> bool:
        path = self.who_genes_edit.text().strip()
        try:
            count = len(load_who_driver_genes(path))
        except (OSError, ValueError) as exc:
            self.who_path_status.setText(str(exc))
            self.who_path_status.setStyleSheet(f"color: {Palette.red};")
            return False
        source = "Innebygd liste" if not path else Path(path).name
        self.who_path_status.setText(f"{source}: {count} drivergener. Brukes i pasientvedlegget.")
        self.who_path_status.setStyleSheet(f"color: {Palette.green};")
        return True

    def _browse_artifact_catalog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Velg artefaktliste", self.artifact_path_edit.text(), "Excel-liste (*.xlsx)",
        )
        if path:
            self.artifact_path_edit.setText(path)
            self._validate_artifact_path()

    def _artifact_source_changed(self) -> None:
        manual = not self.artifact_path_edit.text().strip()
        self.artifact_table.setEnabled(manual)
        for button in self.artifact_edit_buttons:
            button.setEnabled(manual)
        self.artifact_path_status.setText("Listevalget er endret. Lagre innstillinger for å validere.")
        self.artifact_path_status.setStyleSheet("")

    def _validate_artifact_path(self) -> bool:
        self._artifact_source_changed()
        path = self.artifact_path_edit.text().strip()
        try:
            count = len(load_artifact_rules(path, fallback=self._artifact_rules_from_table()))
        except (OSError, ValueError) as exc:
            self.artifact_path_status.setText(str(exc))
            self.artifact_path_status.setStyleSheet(f"color: {Palette.red};")
            return False
        source = Path(path).name if path else "Lagrede / innebygde regler"
        self.artifact_path_status.setText(
            f"{source}: {count} regler. Excel-endringer leses ved neste behandling / gjenåpning."
        )
        self.artifact_path_status.setStyleSheet(f"color: {Palette.green};")
        return True

    def _load_artifact_table(self, artifacts: list[dict[str, str]]) -> None:
        self.artifact_table.setRowCount(0)
        for artifact in artifacts:
            row = self.artifact_table.rowCount()
            self.artifact_table.insertRow(row)
            for col, key in enumerate(["gene", "hgvsc", "max_af", "reason"]):
                self.artifact_table.setItem(row, col, QTableWidgetItem(str(artifact.get(key) or "")))

    def _artifact_rules_from_table(self) -> list[dict[str, str]]:
        artifacts = []
        for row in range(self.artifact_table.rowCount()):
            gene = self._table_text(self.artifact_table, row, 0).upper()
            hgvsc = self._table_text(self.artifact_table, row, 1)
            max_af = self._table_text(self.artifact_table, row, 2)
            reason = self._table_text(self.artifact_table, row, 3)
            if hgvsc:
                artifact = {"gene": gene, "hgvsc": hgvsc, "reason": reason}
                if max_af:
                    artifact["max_af"] = max_af
                artifacts.append(artifact)
        return artifacts

    def _add_artifact_row(self) -> None:
        row = self.artifact_table.rowCount()
        self.artifact_table.insertRow(row)
        for col in range(4):
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
        if self._operation_active or self._import_ready_draft == self._import_draft():
            return
        input_path = Path(self.input_edit.text())
        output_path = Path(self.output_edit.text())
        if not input_path.is_file() or not self.output_edit.text().strip():
            QMessageBox.warning(self, "Missing files", "Select an input TSV and output workbook.")
            return
        if output_path.suffix.lower() != ".xlsx":
            output_path = output_path.with_suffix(".xlsx")
            self.output_edit.setText(str(output_path))
        if not self._save_settings(silent=True):
            return
        self._start_run_journal(output_path.parent, "processing")
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
        thread.finished.connect(self._worker_thread_finished)
        self.processing_thread = thread
        self.processing_worker = worker
        thread.start()

    def _processing_finished(self, result: ProcessingResult) -> None:
        self.status_matrix.clearSelection()
        self.status_matrix.unselect_all_patients()
        self.status_matrix.setCurrentCell(-1, -1)
        self.latest_action_label.setText("Klar til søk")
        self.result = result
        self._import_ready_draft = self._import_draft()
        self._import_excel_opened = False
        self.evidence = {}
        self.database_skip_keys = set()
        self.report_outcomes = {}
        self._pending_report_after_workbook = []
        self.resume_edit.clear()
        self.resume_status.setText("Ny review-fil er opprettet fra TSV.")
        self.selection_status.setText("Ingen X-valg lastet inn")
        self._log(f"Complete: {result.total_count} variants, {len(result.included)} included, {len(result.excluded)} excluded")
        self._refresh_metrics()
        self.open_workbook_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.search_btn.setText('Søker…')
        self.browser_review_btn.setEnabled(True)
        self.rewrite_btn.setEnabled(True)
        self.patient_excel_btn.setEnabled(True)
        self.load_selection_btn.setEnabled(True)
        if result.output_path is not None:
            self._remember_recent_workbook(result.output_path)
        self._set_ready()
        QMessageBox.information(self, "Complete", f"Workbook saved:\n{result.output_path}")

    def _start_prioritized_search(self) -> None:
        patient_ids = self._explicitly_selected_patient_ids()
        if not patient_ids:
            QMessageBox.warning(
                self,
                "Ingen pasienter valgt",
                "Marker én eller flere pasientrader før prioritert kjøring.",
            )
            return
        self._start_database_search(
            patient_ids=set(patient_ids),
            report_after_search=patient_ids,
            scope_label="prioriterte pasienter",
        )

    def _start_remaining_search(self) -> None:
        self._start_database_search(scope_label="resterende pasienter")

    def _start_selected_retry(self) -> None:
        patient_ids = self._explicitly_selected_patient_ids()
        if not patient_ids:
            return
        self._start_database_search(
            patient_ids=set(patient_ids),
            report_after_search=patient_ids,
            scope_label="feilede oppslag for valgte pasienter",
            retry_failed_only=True,
        )

    def _start_database_search(
        self,
        *,
        patient_ids: set[str] | None = None,
        report_after_search: list[str] | None = None,
        scope_label: str = "alle pasienter",
        retry_failed_only: bool = False,
    ) -> None:
        if self._operation_active or not self.result:
            return
        if not self._save_settings(silent=True):
            return
        databases = [name for name, check in self.db_checks.items() if check.isChecked()]
        if not databases:
            QMessageBox.warning(self, "No sources", "Select at least one evidence source.")
            return
        browser_databases = self._selected_browser_databases()
        api_databases: list[str] = []
        completed_sources = _completed_evidence_sources(self.evidence)
        eligible_variants = self._variants_for_search(patient_ids=patient_ids)
        if retry_failed_only:
            retry_pairs = _retryable_source_pairs(
                eligible_variants, self.evidence, databases
            )
            completed_sources.difference_update(retry_pairs)
            completed_sources.update(
                (BrowserReviewService.variant_key(variant), database)
                for variant in eligible_variants
                for database in databases
                if (BrowserReviewService.variant_key(variant), database)
                not in retry_pairs
            )
            variants = [
                variant
                for variant in eligible_variants
                if any(
                    (BrowserReviewService.variant_key(variant), database)
                    in retry_pairs
                    for database in databases
                )
            ]
        else:
            variants = self._pending_variants_for_search(
                databases, patient_ids=patient_ids
            )
        if not variants:
            if retry_failed_only:
                self._log("Ingen feilede oppslag for valgte pasienter og kilder.")
                return
            self._show_search_already_complete(databases)
            if report_after_search and self._try_write_evidence_workbook(
                show_errors=False
            ):
                self._start_patient_reports(report_after_search)
            return
        total_units = len(eligible_variants) * len(databases)
        pending_units = sum(
            (BrowserReviewService.variant_key(variant), database)
            not in completed_sources
            for variant in eligible_variants
            for database in databases
        )
        self._search_started_at = time.monotonic()
        output_directory = (
            self.result.output_path.parent
            if self.result.output_path is not None
            else Path(self.settings.default_output_dir)
        )
        self._start_run_journal(output_directory, "evidence")
        self._active_search_report_patient_ids = list(report_after_search or [])
        self._prepare_search_status(variants, databases, completed_sources)
        self._set_busy("Searching")
        self.search_btn.setText('Søker…')
        self._log(
            f"Resume-aware scope for {scope_label}: "
            f"{len(variants)}/{len(eligible_variants)} variant(s), "
            f"{pending_units}/{total_units} source lookup(s) pending"
        )
        pending_by_source = _pending_source_counts(
            eligible_variants, databases, completed_sources
        )
        self._log(
            " | ".join(
                [
                    "PENDING",
                    f"scope={scope_label}",
                    f"total={pending_units}",
                    *(f"{database}={count}" for database, count in pending_by_source.items()),
                ]
            )
        )
        worker = DatabaseWorker(
            variants,
            api_databases,
            browser_databases,
            self._browser_artifact_root(),
            self.settings,
            completed_sources,
            self._patient_indexes(),
            self.result,
            self.evidence,
            eligible_variants,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status.connect(self._log)
        worker.progress.connect(self._update_run_progress)
        worker.paused.connect(self._search_pause_changed)
        worker.patient_finished.connect(self._database_patient_finished)
        worker.evidence_updated.connect(self._search_evidence_updated)
        worker.source_state.connect(self._source_state_changed)
        worker.report_outcome.connect(self._patient_report_outcome)
        if hasattr(worker, "activity"):
            worker.activity.connect(self._activity_received)
        worker.finished.connect(self._database_finished)
        worker.cancelled.connect(self._search_cancelled)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_thread_finished)
        self.database_thread = thread
        self.database_worker = worker
        thread.start()

    def _database_finished(self, evidence: dict) -> None:
        report_patient_ids = list(self._active_search_report_patient_ids)
        self._active_search_report_patient_ids = []
        self._pending_report_after_workbook = report_patient_ids
        _merge_evidence_results(self.evidence, evidence)
        self._log(_evidence_completion_summary(self.evidence))
        self._refresh_operations_cockpit()
        self._queue_evidence_workbook_write()
        self._finish_search_workbook_checkpoint()
        self._complete_run_progress("Evidence search complete")
        self.search_btn.setText('Søker…')
        self._log(
            f"Patient-by-patient evidence search complete ({self._search_elapsed_text()})"
        )
        if report_patient_ids:
            self._log(
                "Prioriterte rapporter venter på siste lagrede versjon av "
                "evidensarbeidsboken."
            )

    def _database_patient_finished(self, patient_evidence: dict) -> None:
        _merge_evidence_results(self.evidence, patient_evidence)
        self._update_evidence_summary()
        self._auto_rewrite_workbook()

    def _browser_recovery_blocked(self) -> bool:
        return self._operation_active or any(
            self._thread_is_running(getattr(self, name, None))
            for name in (
                "processing_thread", "workbook_load_thread", "workbook_write_thread",
                "patient_report_thread", "report_retry_thread", "database_thread",
                "browser_thread", "session_check_thread",
            )
        )

    def _save_browser_settings(self) -> bool:
        """Persist browser options without accepting unsaved reference paths."""
        minimum_delay = self.browser_delay_spin.value()
        maximum_delay = max(minimum_delay, self.browser_delay_max_spin.value())
        candidate = replace(
            self.settings,
            cosmic_email=self.cosmic_email_edit.text(),
            cosmic_password=self.cosmic_password_edit.text(),
            oncokb_email=self.oncokb_email_edit.text(),
            oncokb_password=self.oncokb_password_edit.text(),
            franklin_email=self.franklin_email_edit.text(),
            franklin_password=self.franklin_password_edit.text(),
            mtbp_email=self.mtbp_email_edit.text(),
            mtbp_password=self.mtbp_password_edit.text(),
            browser_delay_seconds=minimum_delay,
            browser_delay_max_seconds=maximum_delay,
            browser_background=self.browser_background_check.isChecked(),
            mtbp_timeout_minutes=self.mtbp_timeout_spin.value(),
            mtbp_cancer_type=self.mtbp_cancer_type_edit.text().strip() or "Blood",
        )
        try:
            candidate.save()
        except (OSError, CredentialStoreError) as exc:
            QMessageBox.warning(self, "Kunne ikke lagre innlogging", str(exc))
            return False
        self.settings = candidate
        self.browser_delay_max_spin.setValue(maximum_delay)
        return True

    def _start_browser_login(self, database: str | None = None) -> None:
        if self._browser_recovery_blocked():
            return
        # QPushButton.clicked supplies its checked flag to optional arguments.
        if isinstance(database, bool):
            database = None
        database = self.browser_database_combo.currentText() if database is None else database
        if database not in BROWSER_DATABASES:
            raise ValueError(f"Unknown evidence provider: {database!r}")
        if not self._save_browser_settings():
            return
        self._browser_login_database = database
        self._set_busy(f"{database} sign-in")
        worker = BrowserLoginWorker(database, self.settings)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status.connect(self._log)
        worker.finished.connect(self._browser_login_finished)
        worker.failed.connect(self._browser_login_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_thread_finished)
        thread.finished.connect(self._set_ready)
        self.browser_thread = thread
        self.browser_worker = worker
        thread.start()

    def _browser_login_finished(self, message: str) -> None:
        self._log(message)
        database = getattr(self, "_browser_login_database", self.browser_database_combo.currentText())
        self._session_results.pop(database, None)
        self.session_status_labels[database].setText("Sjekk på nytt")
        self.session_status_labels[database].setStyleSheet("")
        self.session_status_labels[database].setToolTip("Innlogging kan være endret siden forrige sjekk.")
        self.session_check_summary.setText("Innlogging kan være endret. Bruk Sjekk innlogging for å bekrefte tilgangen.")
        self._session_controls_changed()
        self._session_release_if_stopped(self.browser_thread)

    def _browser_login_failed(self, message: str) -> None:
        database = self._browser_login_database
        self._session_results.pop(database, None)
        label = self.session_status_labels[database]
        label.setText("Sjekk på nytt")
        label.setStyleSheet("")
        label.setToolTip(message)
        self.session_check_summary.setText(f"{database}: innlogging feilet. Sjekk tilgang på nytt etter retting.")
        self._log(f"{database} sign-in failed: {message}")
        self._session_controls_changed()
        QMessageBox.critical(self, "Innlogging feilet", message)
        self._session_release_if_stopped(self.browser_thread)

    def _start_session_check(self) -> None:
        self._check_browser_sessions()

    def _check_browser_sessions(self, database: str | None = None) -> None:
        if self._browser_recovery_blocked():
            return
        if database is not None and database not in BROWSER_DATABASES:
            raise ValueError(f"Unknown evidence provider: {database!r}")
        scope = tuple(self.databases) if database is None else (database,)
        self._session_check_scope = scope
        self._set_busy("Checking browser sessions")
        self.session_check_summary.setText(
            "Sjekker alle fem kilder i Edge …" if database is None else f"Sjekker {database} i Edge …"
        )
        for provider in scope:
            self._session_results.pop(provider, None)
            label = self.session_status_labels[provider]
            label.setText("Sjekker …")
            label.setStyleSheet("")
            label.setToolTip("")
        worker = BrowserSessionCheckWorker(database)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.checked.connect(self._session_checked)
        worker.finished.connect(self._session_check_finished)
        worker.failed.connect(self._session_check_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_thread_finished)
        thread.finished.connect(self._set_ready)
        self.session_check_worker = worker
        self.session_check_thread = thread
        thread.start()

    def _session_controls_changed(self) -> None:
        update = getattr(self, "_update_source_access_controls", None)
        if callable(update):
            update()

    def _session_release_if_stopped(self, thread) -> None:
        # Terminal results can reach the GUI before queued quit/delete slots.
        # Running recovery threads release controls from their finished signal.
        if not self._thread_is_running(thread):
            self._set_ready()

    def _session_checked(self, result) -> None:
        self._session_results[result.database] = result
        text, color = {
            "authenticated": ("Innlogget", Palette.green),
            "public": ("Offentlig tilgang", Palette.blue),
            "login_required": ("Krever innlogging", Palette.yellow),
            "unknown": ("Ikke bekreftet", Palette.yellow),
            "error": ("Kunne ikke sjekke", Palette.red),
        }.get(result.status, ("Ikke bekreftet", Palette.yellow))
        label = self.session_status_labels[result.database]
        label.setText(text)
        label.setStyleSheet(f"color: {color};")
        label.setToolTip(f"Sjekket {datetime.now():%H:%M:%S}\n{result.message}")
        self._log(f"{result.database}: {text} — {result.message}")
        self._session_controls_changed()

    def _session_check_finished(self, results) -> None:
        ready = sum(item.status in {"authenticated", "public"} for item in results)
        scope = getattr(self, "_session_check_scope", tuple(self.databases))
        detail = (
            "Tilgangen er bekreftet."
            if ready == len(scope)
            else "Se status ved kilden. Logg inn ved behov og sjekk igjen."
        )
        source = f"{scope[0]} · " if len(scope) == 1 else ""
        self.session_check_summary.setText(f"Sjekket {datetime.now():%H:%M} · {source}{ready}/{len(scope)} klare. {detail}")
        self._session_controls_changed()
        self._session_release_if_stopped(self.session_check_thread)

    def _session_check_failed(self, message: str) -> None:
        for database in getattr(self, "_session_check_scope", tuple(self.databases)):
            if database not in self._session_results:
                label = self.session_status_labels[database]
                label.setText("Kunne ikke sjekke")
                label.setToolTip(message)
                label.setStyleSheet(f"color: {Palette.red};")
        self.session_check_summary.setText(f"Innloggingssjekken feilet: {message}")
        self._log(f"Browser session check failed: {message}")
        self._session_controls_changed()
        self._session_release_if_stopped(self.session_check_thread)

    def _start_browser_review(self) -> None:
        if self._operation_active:
            return
        if not self.result:
            return
        if not self._save_settings(silent=True):
            return
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
        return [
            database
            for database in BROWSER_DATABASES
            if self.db_checks[database].isChecked()
        ]

    def _launch_browser_review(
        self, databases: list[str], *, variants: list | None = None
    ) -> None:
        if not self.result:
            return
        completed_sources = _completed_evidence_sources(self.evidence)
        if variants is None:
            variants = self._pending_variants_for_search(databases)
            if not variants:
                self._show_search_already_complete(databases)
                return
        self._search_started_at = time.monotonic()
        output_directory = (
            self.result.output_path.parent
            if self.result.output_path is not None
            else Path(self.settings.default_output_dir)
        )
        self._start_run_journal(output_directory, "browser_evidence")
        self._prepare_search_status(variants, databases, completed_sources)
        self._set_busy("Browser lookups")
        worker = BrowserReviewWorker(
            variants,
            databases,
            self._browser_artifact_root(),
            self.settings,
            completed_sources,
            self._patient_indexes(),
            self.result,
            self.evidence,
            self._variants_for_search(),
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status.connect(self._log)
        worker.progress.connect(self._update_run_progress)
        worker.paused.connect(self._search_pause_changed)
        worker.patient_finished.connect(self._browser_patient_finished)
        worker.evidence_updated.connect(self._search_evidence_updated)
        worker.source_state.connect(self._source_state_changed)
        worker.report_outcome.connect(self._patient_report_outcome)
        worker.activity.connect(self._activity_received)
        worker.finished.connect(self._browser_review_finished)
        worker.cancelled.connect(self._search_cancelled)
        worker.failed.connect(self._browser_review_failed)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_thread_finished)
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

    def _variants_for_search(self, *, patient_ids: set[str] | None = None):
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
            if (patient_ids is None or variant.patient_id in patient_ids)
            and BrowserReviewService.variant_key(variant) not in self.database_skip_keys
            and not is_automatic_database_skip(variant)
        ]

    def _pending_variants_for_search(
        self,
        databases: list[str],
        *,
        patient_ids: set[str] | None = None,
    ):
        completed_sources = _completed_evidence_sources(self.evidence)
        return [
            variant
            for variant in self._variants_for_search(patient_ids=patient_ids)
            if any(
                (BrowserReviewService.variant_key(variant), database)
                not in completed_sources
                for database in databases
            )
        ]

    def _patient_indexes(self) -> dict[str, int]:
        if not self.result:
            return {}
        return {
            patient_id: index
            for index, patient_id in enumerate(
                dict.fromkeys(
                    variant.patient_id for variant in self.result.variants
                ),
                start=1,
            )
        }

    def _show_search_already_complete(self, databases: list[str]) -> None:
        source_text = ", ".join(databases)
        self.run_progress.show()
        self.run_progress.title.setText("Selected evidence is already complete")
        self.run_progress.detail.setText(
            "There is no unfinished work for the selected sources. Completed evidence "
            "will not be repeated."
        )
        self._set_search_complete_status(save_pending=False)
        self.status_badge.setText("Evidence complete")
        self.search_btn.setText('Søker…')
        self._log(f"Nothing to resume; selected sources already complete: {source_text}")

    def _load_database_selection(self) -> None:
        if self._operation_active or not self.result:
            return
        initial = str(self.result.output_path or self.settings.default_output_dir)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Hent X-valg fra review-filen",
            initial,
            "Excel workbook (*.xlsx)",
        )
        if not path:
            return
        try:
            loaded = load_database_skip_keys(Path(path))
        except Exception as exc:
            QMessageBox.critical(self, "Kunne ikke hente X-valg", str(exc))
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
            f"{len(self.database_skip_keys)} X-valg · {searched} varianter til søk"
        )
        if unmatched:
            message += f" · {unmatched} markeringer tilhører ikke denne analysen"
        self.selection_status.setText(f"Hentet {datetime.now():%H:%M} · {message}")
        self._update_evidence_summary()
        self._log(f"Database selection loaded: {message}")

    def _browse_processed_workbook(self) -> None:
        start_dir = (
            self.result.output_path.parent
            if self.result and self.result.output_path
            else Path(self.settings.default_output_dir)
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Processed VPM Workbook",
            str(start_dir),
            "Excel Workbook (*.xlsx)",
        )
        if path:
            self._load_processed_workbook(Path(path))

    def _load_processed_workbook(self, workbook_path: Path) -> None:
        if self._operation_active:
            return
        if self._thread_is_running(self.workbook_load_thread):
            return
        self._start_run_journal(workbook_path.parent, "resume")
        self._set_busy("Loading workbook")
        self.run_progress.show()
        self.run_progress.title.setText("Loading processed workbook")
        worker = ProcessedWorkbookWorker(workbook_path, self.settings)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._processed_workbook_progress)
        worker.finished.connect(self._processed_workbook_loaded)
        worker.failed.connect(self._processed_workbook_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_thread_finished)
        self.workbook_load_worker = worker
        self.workbook_load_thread = thread
        thread.start()

    def _processed_workbook_progress(
        self, current: int, total: int, detail: str
    ) -> None:
        self.run_progress.update_progress(current, total, detail)

    def _processed_workbook_loaded(self, workbook_path: Path, state) -> None:
        self.status_matrix.clearSelection()
        self.status_matrix.unselect_all_patients()
        self.status_matrix.setCurrentCell(-1, -1)
        self.latest_action_label.setText("Klar til søk")
        self.result = state.result
        self.evidence = state.evidence
        self.database_skip_keys = state.database_skip_keys
        self.report_outcomes = {}
        self._pending_report_after_workbook = []
        self.import_guidance.setText("Analysen er gjenopprettet. Fortsett uferdige oppslag på Evidence-siden.")
        self.workbook_write_pending = False
        self._workbook_lock_warning_shown = False
        self.output_edit.setText(str(workbook_path))
        self.resume_edit.setText(str(workbook_path))
        self._import_ready_draft = self._import_draft()
        self._import_excel_opened = True
        self._set_import_mode(1)
        self.included_only_check.setChecked(False)
        evidence_count = sum(len(items) for items in self.evidence.values())
        searched = self.result.total_count - len(self.database_skip_keys)
        self.selection_status.setText(
            f"{len(self.database_skip_keys)} X-valg lastet inn · {searched} varianter til søk"
        )
        self.resume_status.setText(
            f"Gjenåpnet {self.result.total_count} varianter, "
            f"{len(self.database_skip_keys)} X-valg og "
            f"{evidence_count} oppslagsresultater."
        )
        self.search_btn.setText(
            "Resume Incomplete Search" if self.evidence else "Run Evidence Search"
        )
        self._refresh_metrics()
        self.open_workbook_btn.setEnabled(True)
        self._refresh_operations_cockpit()
        self.load_selection_btn.setEnabled(True)
        self._remember_recent_workbook(workbook_path)
        self._set_ready()
        self.status_badge.setText("Workbook loaded")
        self.status_badge.setStyleSheet(
            f"background: {Palette.pale_green}; color: {Palette.green}; "
            f"border: 1px solid {Palette.green}; border-radius: 12px; "
            "padding: 5px 12px; font-weight: 700;"
        )
        self._log(f"Processed workbook restored: {workbook_path}")
        self._switch_page(1)
        QMessageBox.information(
            self,
            "Analysis restored",
            f"Loaded {self.result.total_count} variants and {evidence_count} evidence result(s).\n\n"
            "The analysis is ready in Evidence.",
        )

    def _remember_recent_workbook(self, workbook_path: Path) -> None:
        self.settings.last_processed_workbook = str(workbook_path)
        try:
            self.settings.save()
        except (OSError, CredentialStoreError) as exc:
            self._log(f"Kunne ikke lagre nylig analyse: {exc}")
        self.recent_analysis_panel.hide()

    def _processed_workbook_failed(self, message: str) -> None:
        self._set_ready()
        self._log(f"Could not resume processed workbook: {message}")
        QMessageBox.critical(
            self,
            "Processed workbook could not be loaded",
            f"{message}\n\nChoose a workbook created by the current VPM Tolkning review workflow.",
        )

    def _browser_review_finished(self, browser_evidence: dict) -> None:
        self._merge_browser_evidence(browser_evidence)
        self._refresh_operations_cockpit()
        self._auto_rewrite_workbook()
        self._finish_search_workbook_checkpoint()
        self._complete_run_progress("Browser evidence complete")
        self.search_btn.setText('Søker…')
        self._log(f"Browser evidence lookup complete ({self._search_elapsed_text()})")

    def _browser_patient_finished(self, patient_evidence: dict) -> None:
        self._merge_browser_evidence(patient_evidence)
        self._update_evidence_summary()
        self._auto_rewrite_workbook()

    def _patient_report_outcome(self, outcome: PatientReportOutcome) -> None:
        self.report_outcomes[outcome.patient_id] = outcome
        self._refresh_operations_cockpit()
        if outcome.status in {"created", "updated"}:
            self._log(f"Patient workbook ready: {outcome.path}")
        elif outcome.status == "locked":
            self._log(
                f"Patient workbook locked for {outcome.patient_id}; "
                "close it in Excel and retry."
            )
        else:
            self._log(f"Patient workbook failed for {outcome.patient_id}: {outcome.message}")

    def _merge_browser_evidence(self, browser_evidence: dict) -> None:
        _merge_evidence_results(self.evidence, browser_evidence)
        self._log(_evidence_completion_summary(self.evidence))

    def _browser_review_failed(self, message: str) -> None:
        self._worker_failed(message)

    def _stop_evidence_search(self) -> None:
        requested = False
        for thread, worker in (
            (self.database_thread, self.database_worker),
            (self.browser_thread, self.browser_worker),
        ):
            if (
                self._thread_is_running(thread)
                and isinstance(worker, (DatabaseWorker, BrowserReviewWorker))
            ):
                thread.requestInterruption()
                worker.resume_search()
                requested = True
        if not requested:
            return
        self._search_stop_requested = True
        self._search_pause_requested = False
        self.pause_search_btn.setEnabled(False)
        self.pause_search_btn.setText("Pause")
        self.stop_search_btn.setEnabled(False)
        self.stop_search_btn.setText("Stopper…")
        self.run_progress.show()
        self.run_progress.title.setText("Stopping evidence search")
        self.run_progress.detail.setText(
            "Finishing the current safe browser action. Evidence already collected will be kept."
        )
        self.status_badge.setText("Stopping")
        self.run_status_strip.set_snapshot(RunSnapshot(phase=RunPhase.STOPPING))
        self.status_badge.setText("Stopping")
        self.status_badge.setStyleSheet(
            f"background: {Palette.pale_yellow}; color: {Palette.yellow}; "
            "border: 1px solid #E7CF91; border-radius: 12px; "
            "padding: 5px 12px; font-weight: 700;"
        )
        self._log("Stop requested; waiting for the current safe browser action")

    def _toggle_search_pause(self) -> None:
        active_workers = []
        for thread, worker in (
            (self.database_thread, self.database_worker),
            (self.browser_thread, self.browser_worker),
        ):
            if (
                self._thread_is_running(thread)
                and isinstance(worker, (DatabaseWorker, BrowserReviewWorker))
            ):
                active_workers.append(worker)
        if not active_workers:
            return
        if self._search_pause_requested:
            for worker in active_workers:
                worker.resume_search()
            self._search_pause_requested = False
            self.pause_search_btn.setText("Pause")
            self.run_progress.show()
            self.run_progress.title.setText("Resuming evidence search")
            self.run_progress.detail.setText(
                "Continuing from the same patient and source queue."
            )
            self.status_badge.setText("Resuming")
            self.run_status_strip.set_snapshot(RunSnapshot(phase=RunPhase.RUNNING))
            self.status_badge.setText("Resuming")
            self._log("Evidence search resumed from the same queue")
            return
        for worker in active_workers:
            worker.request_pause()
        self._search_pause_requested = True
        self.pause_search_btn.setText("Fortsett")
        self.run_progress.show()
        self.run_progress.title.setText("Pause requested")
        self.run_progress.detail.setText(
            "The queue will pause at the next safe checkpoint in the current browser action."
        )
        self.status_badge.setText("Pausing")
        self.run_status_strip.set_snapshot(RunSnapshot(phase=RunPhase.PAUSING))
        self.status_badge.setText("Pausing")
        self.status_badge.setStyleSheet(
            f"background: {Palette.pale_yellow}; color: {Palette.yellow}; "
            "border: 1px solid #E7CF91; border-radius: 12px; "
            "padding: 5px 12px; font-weight: 700;"
        )
        self._log("Pause requested; waiting for a safe checkpoint")

    def _search_pause_changed(self, paused: bool) -> None:
        if self._search_stop_requested:
            return
        if paused:
            self.activity_progress.hide()
            self.run_progress.show()
            self.run_progress.title.setText("Evidence search paused")
            self.run_progress.detail.setText(
                "Completed evidence is safe. Resume continues from this exact queue position."
            )
            self.status_badge.setText("Paused")
            self.run_status_strip.set_snapshot(RunSnapshot(phase=RunPhase.PAUSED))
            self.status_badge.setText("Paused")
            self.status_badge.setStyleSheet(
                f"background: {Palette.pale_yellow}; color: {Palette.yellow}; "
                "border: 1px solid #E7CF91; border-radius: 12px; "
                "padding: 5px 12px; font-weight: 700;"
            )
            self.status_bar.showMessage("Evidence search paused")
            return
        self.status_badge.setText("Searching")
        self.status_badge.setStyleSheet(
            f"background: {Palette.pale_blue}; color: {Palette.blue}; "
            f"border: 1px solid {Palette.blue}; border-radius: 12px; "
            "padding: 5px 12px; font-weight: 700;"
        )
        self.status_bar.showMessage("Evidence search resumed")

    def _search_cancelled(self) -> None:
        self._active_search_report_patient_ids = []
        self._refresh_operations_cockpit()
        self._auto_rewrite_workbook()
        self._finish_search_workbook_checkpoint()
        self.run_progress.show()
        self.run_progress.title.setText("Evidence search stopped")
        detail = (
            "Completed source results were kept and written to the review workbook. "
            "Resume Incomplete Search continues with only unfinished work."
        )
        if self.workbook_write_thread is not None:
            detail = "Completed results were kept. Saving them to the review workbook before the next operation."
        elif self.workbook_write_pending:
            detail = (
                "Completed results were kept, but the workbook is open in Excel. "
                "Close it, then click Update Review Workbook."
            )
        self.run_progress.detail.setText(detail)
        self.status_badge.setText("Search stopped")
        self.status_badge.setStyleSheet(
            f"background: {Palette.pale_yellow}; color: {Palette.yellow}; "
            "border: 1px solid #E7CF91; border-radius: 12px; "
            "padding: 5px 12px; font-weight: 700;"
        )
        self.status_bar.showMessage("Evidence search stopped — completed results kept")
        self._update_priority_controls()
        self._log(
            f"Evidence search stopped; completed results retained "
            f"({self._search_elapsed_text()})"
        )

    def _rewrite_workbook(self) -> None:
        if not self.result or not self.result.output_path:
            return
        if self.workbook_write_thread is not None:
            self._workbook_write_requested = True
            self._log(
                "Manual workbook update queued behind the active background write."
            )
            return
        if not self._try_write_evidence_workbook(show_errors=True):
            return
        if not self.run_progress.isHidden() and "complete" in self.run_progress.title.text().casefold():
            self.run_progress.detail.setText(
                "All queued patients have been processed and the workbook is updated."
            )
            self._set_search_complete_status(save_pending=False)
        QMessageBox.information(self, "Workbook Updated", f"Evidence written to:\n{self.result.output_path}")
        patient_ids = self._pending_report_after_workbook
        self._pending_report_after_workbook = []
        if patient_ids:
            self._start_patient_reports(patient_ids)

    def _selected_patient_ids(self) -> list[str]:
        if self.result is None:
            return []
        selected = self._explicitly_selected_patient_ids()
        if selected:
            return selected
        return sorted({variant.patient_id for variant in self.result.variants})

    def _explicitly_selected_patient_ids(self) -> list[str]:
        return self.status_matrix.selected_patients() if self.result else []


    def _update_priority_controls(self) -> None:
        if not hasattr(self, 'priority_search_btn'):
            return
        selected = self._explicitly_selected_patient_ids()
        count = len(selected)
        total = len({v.patient_id for v in self.result.variants}) if self.result else 0
        self.priority_selection_status.setText(
            f'{count} av {total} pasienter avkrysset' if count else 'Ingen avkrysset · søk gjelder alle'
        )
        blocker = self.select_all_patients_check.blockSignals(True)
        self.select_all_patients_check.setChecked(bool(total and count == total))
        self.select_all_patients_check.blockSignals(blocker)
        self.select_all_patients_check.setEnabled(bool(total))
        available = self.result is not None and not self._operation_active
        sources = [db for db, check in self.db_checks.items() if check.isChecked()]
        scope = set(selected) if selected else None
        pending = self._pending_variants_for_search(sources, patient_ids=scope) if self.result and sources else []
        self.priority_search_btn.setEnabled(available and bool(count) and bool(sources))
        retry_pairs = _retryable_source_pairs(self._variants_for_search(patient_ids=set(selected)), self.evidence, sources) if available and selected and sources else set()
        self.retry_selected_btn.setEnabled(bool(retry_pairs))
        self.retry_selected_btn.setText(f'Prøv feilede på nytt ({len(retry_pairs)})' if retry_pairs else 'Prøv feilede på nytt')
        self.remaining_search_btn.setEnabled(available and bool(sources) and bool(self._pending_variants_for_search(sources)))
        self.search_btn.setEnabled(available and bool(pending))
        self.search_btn.setText(f'Søk {count} valgte pasienter' if count else f'Søk {len(pending)} ventende varianter')
        self.search_btn.setToolTip('Fullførte oppslag beholdes. ' + ('Valgte pasienter prioriteres, og vedlegg lages etter lagring.' if count else 'Søker bare uferdige oppslag for alle pasienter.'))
        if self.result:
            self.patient_excel_btn.setText(f'Vedlegg for {count} valgt' if count == 1 else f'Vedlegg for {count} valgte' if count else f'Vedlegg for alle {total}')
        else:
            self.patient_excel_btn.setText('Generer vedlegg')
        self.patient_details_btn.setEnabled(bool(self.status_matrix.focused_patient()))


    @staticmethod
    def _report_outcome_summary(outcomes: list[PatientReportOutcome]) -> str:
        counts = {
            "created": 0,
            "updated": 0,
            "locked": 0,
            "failed": 0,
        }
        for outcome in outcomes:
            if outcome.status in counts:
                counts[outcome.status] += 1
        return (
            f"Opprettet: {counts['created']} · Oppdatert: {counts['updated']} · "
            f"Hoppet over/låst: {counts['locked']} · Feilet: {counts['failed']}"
        )

    def _export_patient_excels(self) -> None:
        if not self.result or not self.result.output_path:
            return
        patient_ids = self._selected_patient_ids()
        if not patient_ids:
            QMessageBox.warning(
                self,
                "Ingen rapporter å generere",
                "Ingen pasienter er tilgjengelige for rapportgenerering.",
            )
            return
        self._start_patient_reports(patient_ids)

    def _start_patient_reports(self, patient_ids: list[str]) -> None:
        if not self.result or not self.result.output_path or not patient_ids:
            return
        try:
            who_genes = load_who_driver_genes(self.settings.who_driver_genes_path)
        except (OSError, ValueError) as exc:
            self._pending_report_after_workbook = list(patient_ids)
            self._set_ready()
            self._log(f"Rapportgenerering venter på gyldig WHO-drivergenliste: {exc}")
            QMessageBox.warning(self, "WHO-drivergenlisten kunne ikke leses", str(exc))
            return
        self._set_busy("Generating reports")
        coordinator = PatientReportCoordinator(
            self.result, self.result.variants, self.evidence,
            writer=PatientExcelReportWriter(who_genes),
        )
        worker = PatientReportWorker(coordinator, patient_ids)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._patient_report_progress)
        worker.finished.connect(self._patient_report_finished)
        worker.failed.connect(self._patient_report_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_thread_finished)
        self.patient_report_worker = worker
        self.patient_report_thread = thread
        self.patient_excel_btn.setEnabled(False)
        self._log(f"Genererer VEDLEGG_APP for {len(patient_ids)} pasient(er)")
        thread.start()

    def _patient_report_progress(self, current: int, total: int, patient_id: str) -> None:
        self._log(f"VEDLEGG_APP {current}/{total}: {patient_id}")

    def _patient_report_finished(
        self, outcomes: list[PatientReportOutcome]
    ) -> None:
        for outcome in outcomes:
            self._patient_report_outcome(outcome)
            if outcome.status in {"locked", "failed"}:
                self._log(f"{outcome.status}: {outcome.path} — {outcome.message}")
        self._set_ready()
        summary = self._report_outcome_summary(outcomes)
        self._log(summary)
        QMessageBox.information(self, "VEDLEGG_APP ferdig", summary)

    def _patient_report_failed(self, message: str) -> None:
        self._worker_failed(message)

    def _write_evidence_workbook(self) -> None:
        if not self.result or not self.result.output_path:
            return
        ExcelReportWriter(load_who_driver_genes(self.settings.who_driver_genes_path)).write(
            self.result,
            self.result.output_path,
            self.evidence,
            self.hide_excluded.isChecked(),
            self.database_skip_keys,
        )

    def _auto_rewrite_workbook(self) -> None:
        self._queue_evidence_workbook_write()

    def _finish_search_workbook_checkpoint(self) -> None:
        if self.workbook_write_thread is None:
            self._set_ready()
            return
        # A delayed save must finish its reports before another run can own
        # the analysis. Otherwise the callback could use a newly loaded file.
        self._waiting_final_workbook = True
        self._active_sources.clear()
        self.pause_search_btn.setEnabled(False)
        self.stop_search_btn.setEnabled(False)

    def _queue_evidence_workbook_write(self) -> None:
        if not self.result or not self.result.output_path:
            return
        if self.workbook_write_thread is not None:
            self._workbook_write_requested = True
            return
        self._workbook_write_requested = False
        self._background_workbook_error = None
        self._background_workbook_path = None
        worker = WorkbookWriteWorker(
            self.result,
            self.evidence,
            hide_excluded=self.hide_excluded.isChecked(),
            database_skip_keys=self.database_skip_keys,
            who_driver_genes_path=self.settings.who_driver_genes_path,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._background_workbook_saved)
        worker.failed.connect(self._background_workbook_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._background_workbook_thread_finished)
        self.workbook_write_pending = True
        self._workbook_write_started_at = time.monotonic()
        self.workbook_write_thread = thread
        self.workbook_write_worker = worker
        thread.start()

    def _background_workbook_saved(self, path: Path) -> None:
        self._background_workbook_path = Path(path)

    def _background_workbook_failed(self, exc: Exception) -> None:
        self._background_workbook_error = exc

    def _background_workbook_thread_finished(self) -> None:
        error = self._background_workbook_error
        saved_path = self._background_workbook_path
        write_again = self._workbook_write_requested
        duration = (
            max(0.0, time.monotonic() - self._workbook_write_started_at)
            if self._workbook_write_started_at is not None
            else 0.0
        )
        self.workbook_write_thread = None
        self.workbook_write_worker = None
        self._workbook_write_requested = False
        self._workbook_write_started_at = None
        if error is None and saved_path is not None:
            self.workbook_write_pending = False
            self._workbook_lock_warning_shown = False
            self._log(
                "WORKBOOK WRITE | status=completed | "
                f"duration_seconds={duration:.3f} | path={saved_path}"
            )
        elif error is not None:
            self.workbook_write_pending = True
            self._log(
                "WORKBOOK WRITE | status=failed | "
                f"duration_seconds={duration:.3f} | error_type={type(error).__name__}"
            )
            self._report_workbook_write_error(error, show_errors=False)
        if write_again:
            self._queue_evidence_workbook_write()
            return
        waiting_final = self._waiting_final_workbook
        self._waiting_final_workbook = False
        if error is None and saved_path is not None:
            patient_ids = list(self._pending_report_after_workbook)
            self._pending_report_after_workbook = []
            if patient_ids:
                self._start_patient_reports(patient_ids)
                return
        if waiting_final:
            self._set_ready()
            if "complete" in self.run_progress.title.text().casefold():
                self._complete_run_progress(self.run_progress.title.text())

    def _try_write_evidence_workbook(self, *, show_errors: bool) -> bool:
        try:
            self._write_evidence_workbook()
        except OSError as exc:
            if self._is_workbook_lock_error(exc):
                self.workbook_write_pending = True
                self._log(
                    "Workbook update pending: close the file in Excel, then click "
                    "Rewrite Workbook With Evidence. Evidence remains available in the app."
                )
                if show_errors or not self._workbook_lock_warning_shown:
                    QMessageBox.warning(
                        self,
                        "Workbook is open in Excel",
                        "The workbook could not be updated because it is open or locked.\n\n"
                        "The evidence search will continue and the results remain in the app. "
                        "Close the workbook in Excel, then click Rewrite Workbook With Evidence.",
                    )
                    self._workbook_lock_warning_shown = True
                return False
            self._report_workbook_write_error(exc, show_errors=show_errors)
            return False
        except Exception as exc:
            self._report_workbook_write_error(exc, show_errors=show_errors)
            return False
        self.workbook_write_pending = False
        self._workbook_lock_warning_shown = False
        return True

    @staticmethod
    def _is_workbook_lock_error(exc: OSError) -> bool:
        return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {32, 33}

    def _report_workbook_write_error(self, exc: Exception, *, show_errors: bool) -> None:
        message = f"Could not update the evidence workbook: {exc}"
        self._log(message)
        if show_errors:
            QMessageBox.critical(self, "Workbook update failed", message)

    def _worker_failed(self, message: str) -> None:
        was_search = bool(self._search_pending_pairs)
        if was_search:
            self._record_active_search_failure(message)
            self._auto_rewrite_workbook()
            self._active_search_report_patient_ids = []
            self._finish_search_workbook_checkpoint()
        else:
            self._set_ready()
        if not self.run_progress.isHidden():
            self.run_progress.title.setText("Search stopped")
            self.run_progress.detail.setText(message)
            QTimer.singleShot(5000, self.run_progress.hide)
        if was_search:
            self._update_priority_controls()
            self._log(f"ERROR after {self._search_elapsed_text()}: {message}")
        else:
            self._log(f"ERROR: {message}")
        QMessageBox.critical(self, "Error", message)

    def _save_settings(self, silent: bool = False) -> bool:
        output_dir = self.output_dir_edit.text().strip()
        who_path = self.who_genes_edit.text().strip()
        artifact_path = self.artifact_path_edit.text().strip()
        manual_artifacts = self._artifact_rules_from_table()
        invalid_field = self.output_dir_edit
        try:
            if not output_dir or not Path(output_dir).expanduser().is_dir():
                raise ValueError("Velg en eksisterende mappe for rapporter.")
            output_dir = str(Path(output_dir).expanduser().resolve())
            invalid_field = self.who_genes_edit
            if who_path:
                who_path = str(Path(who_path).expanduser().resolve())
            load_who_driver_genes(who_path)
            invalid_field = self.artifact_path_edit
            if artifact_path:
                artifact_path = str(Path(artifact_path).expanduser().resolve())
            production_rules(load_artifact_rules(artifact_path, fallback=manual_artifacts))
        except (OSError, ValueError) as exc:
            self._validate_who_path()
            self._validate_artifact_path()
            QMessageBox.warning(self, "Ugyldige filinnstillinger", str(exc))
            self._switch_page(2)
            self.settings_sections.setCurrentIndex(0)
            invalid_field.setFocus()
            return False
        candidate = replace(self.settings)
        candidate.default_output_dir = output_dir
        candidate.who_driver_genes_path = who_path
        candidate.artifact_rules_path = artifact_path
        self.output_dir_edit.setText(output_dir)
        self.who_genes_edit.setText(who_path)
        self.artifact_path_edit.setText(artifact_path)
        self._validate_artifact_path()
        candidate.clinvar_api_key = ""
        candidate.cosmic_email = self.cosmic_email_edit.text()
        candidate.cosmic_password = self.cosmic_password_edit.text()
        candidate.oncokb_api_key = ""
        candidate.oncokb_email = self.oncokb_email_edit.text()
        candidate.oncokb_password = self.oncokb_password_edit.text()
        candidate.franklin_api_key = ""
        candidate.franklin_email = self.franklin_email_edit.text()
        candidate.franklin_password = self.franklin_password_edit.text()
        candidate.mtbp_email = self.mtbp_email_edit.text()
        candidate.mtbp_password = self.mtbp_password_edit.text()
        candidate.database_workers = 1
        candidate.browser_delay_seconds = self.browser_delay_spin.value()
        candidate.browser_delay_max_seconds = max(
            candidate.browser_delay_seconds,
            self.browser_delay_max_spin.value(),
        )
        self.browser_delay_max_spin.setValue(
            candidate.browser_delay_max_seconds
        )
        candidate.mtbp_timeout_minutes = self.mtbp_timeout_spin.value()
        candidate.browser_background = self.browser_background_check.isChecked()
        candidate.search_included_only = self.included_only_check.isChecked()
        candidate.mtbp_cancer_type = self.mtbp_cancer_type_edit.text().strip() or "Blood"
        candidate.artifact_rules = manual_artifacts
        candidate.enabled_databases = [name for name, check in self.db_checks.items() if check.isChecked()]
        try:
            candidate.save()
        except (OSError, CredentialStoreError) as exc:
            QMessageBox.warning(self, "Kunne ikke lagre innstillinger", str(exc))
            return False
        self.settings = candidate
        self._update_reference_selection_status()
        if not silent:
            self._log('Innstillinger lagret')
        return True


    def _refresh_metrics(self) -> None:
        if not self.result:
            return
        self._update_evidence_summary()

    def _refresh_operations_cockpit(self) -> None:
        if not hasattr(self, "status_matrix"):
            return
        variants = self.result.variants if self.result else []
        searchable_keys = {
            BrowserReviewService.variant_key(variant)
            for variant in self._variants_for_search()
        }
        skipped_keys = {
            BrowserReviewService.variant_key(variant)
            for variant in variants
            if BrowserReviewService.variant_key(variant) not in searchable_keys
        }
        report_statuses = {
            patient_id: outcome.status
            for patient_id, outcome in self.report_outcomes.items()
        }
        self.status_matrix.set_rows(
            build_patient_status_rows(
                variants,
                databases=self.databases,
                evidence=self.evidence,
                skipped_keys=skipped_keys,
                report_outcomes=report_statuses,
                active=self._active_sources,
                active_keys=self._search_pending_pairs,
                selected_sources=self._selected_queue_sources(),
            )
        )
        self._update_priority_controls()
        if hasattr(self, "retry_report_saves_button"):
            self.retry_report_saves_button.setVisible(
                any(outcome.status == "locked" for outcome in self.report_outcomes.values())
            )

    def _retry_pending_report_saves(self) -> None:
        if self._operation_active:
            return
        if self.result is None or self.result.output_path is None:
            return
        locked = {
            patient_id
            for patient_id, outcome in self.report_outcomes.items()
            if outcome.status == "locked"
        }
        if not locked:
            return
        coordinator = PatientReportCoordinator(
            self.result, self.result.variants, self.evidence,
            writer=PatientExcelReportWriter(
                load_who_driver_genes(self.settings.who_driver_genes_path)
            ),
        )
        coordinator.pending = locked
        worker = ReportRetryWorker(coordinator)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._report_retry_finished)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_thread_finished)
        self.report_retry_thread = thread
        self.report_retry_worker = worker
        self._set_busy("Saving pending reports")
        thread.start()

    def _report_retry_finished(self, outcomes: list[PatientReportOutcome]) -> None:
        for outcome in outcomes:
            self._patient_report_outcome(outcome)
        self._set_ready()

    def _activity_received(self, activity: RunActivity) -> None:
        self._log(activity.message or activity.action)

    def _update_evidence_summary(self) -> None:
        if not hasattr(self, 'evidence_summary'):
            return
        selected = [db for db in self.databases if db in self._selected_queue_sources()]
        variants = len(self._variants_for_search()) if self.result else 0
        pending = len(self._pending_variants_for_search(selected)) if self.result and selected else variants
        if self.result:
            self.evidence_summary.setText(f'{len(selected)} kilder · {pending} av {variants} varianter har uferdige oppslag')
        else:
            self.evidence_summary.setText('Opprett eller åpne en review-fil for å søke.')
        if hasattr(self, 'current_workbook_label'):
            path = self.result.output_path if self.result else None
            self.current_workbook_label.setText(Path(path).name if path else 'Ingen review-fil lastet')
            self.current_workbook_label.setToolTip(str(path) if path else '')
        self._refresh_operations_cockpit()


    def _prepare_search_status(self, variants, databases, completed_sources) -> None:
        self._active_sources.clear()
        self._pending_report_after_workbook = []
        self._queue_databases = set(databases)
        self._search_pending_pairs = {
            (BrowserReviewService.variant_key(variant), database)
            for variant in variants
            for database in databases
            if (BrowserReviewService.variant_key(variant), database) not in completed_sources
        }

    def _selected_queue_sources(self) -> set[str]:
        if self._operation_active and self._queue_databases:
            return self._queue_databases
        return {name for name, check in self.db_checks.items() if check.isChecked()}

    def _source_state_changed(self, patient_id: str, database: str, running: bool) -> None:
        pair = (patient_id, database)
        if running:
            self._active_sources.add(pair)
        else:
            self._active_sources.discard(pair)
        self._refresh_operations_cockpit()

    def _search_evidence_updated(self, evidence: dict) -> None:
        _merge_evidence_results(self.evidence, evidence)
        self._update_evidence_summary()

    def _record_active_search_failure(self, message: str) -> None:
        if self.result is None or not self._active_sources:
            return
        failures = {}
        for variant in self.result.variants:
            key = BrowserReviewService.variant_key(variant)
            by_database = {item.database: item for item in self.evidence.get(key, [])}
            for patient_id, database in self._active_sources:
                if (
                    patient_id != variant.patient_id
                    or (key, database) not in self._search_pending_pairs
                ):
                    continue
                previous = by_database.get(database)
                if previous is not None and is_completed_evidence(previous):
                    continue
                # Preserve MTBP's remote report ID and captures so retry can resume it.
                item = (
                    deepcopy(previous) if previous
                    else DatabaseEvidence(database, "error")
                )
                item.status = (
                    "timeout"
                    if "timed out" in message.casefold() or "timeout" in message.casefold()
                    else "error"
                )
                item.summary = f"Browser lookup interrupted: {message}"
                failures.setdefault(key, []).append(item)
        if failures:
            _merge_evidence_results(self.evidence, failures)

    def _update_run_progress(self, current: int, total: int, detail: str) -> None:
        self.activity_progress.hide()
        self.run_progress.title.setText("Collecting evidence")
        self.run_progress.update_progress(current, total, detail)
        self.run_status_strip.set_snapshot(
            RunSnapshot(
                phase=RunPhase.RUNNING,
                current_patient=current,
                patient_total=total,
                action=detail,
            )
        )

    def _complete_run_progress(self, title: str) -> None:
        self.run_progress.show()
        self.run_progress.title.setText(title)
        detail = "All queued patients have been processed. Results are ready for review."
        if self.workbook_write_thread is not None:
            detail = "Saving the completed results to the review workbook. Priority reports follow after the save."
        elif self.workbook_write_pending:
            detail = (
                "Search finished, but the workbook is still open in Excel. Close it, "
                "then click Rewrite Workbook With Evidence."
            )
        self.run_progress.detail.setText(detail)
        self.run_progress.bar.setValue(self.run_progress.bar.maximum())
        self._set_search_complete_status(save_pending=self.workbook_write_pending)

    def _set_search_complete_status(self, *, save_pending: bool) -> None:
        self.run_status_strip.set_snapshot(
            RunSnapshot(
                phase=(
                    RunPhase.REPORT_PENDING if save_pending else RunPhase.COMPLETE
                ),
                current_patient=self.run_progress.bar.maximum(),
                patient_total=self.run_progress.bar.maximum(),
            )
        )
        if save_pending:
            self.status_badge.setText("Search complete · save pending")
            self.status_badge.setStyleSheet(
                f"background: {Palette.pale_yellow}; color: {Palette.yellow}; "
                "border: 1px solid #E7CF91; border-radius: 12px; "
                "padding: 5px 12px; font-weight: 700;"
            )
            self.status_bar.showMessage("Evidence search complete — workbook update pending")
            return
        self.status_badge.setText("Search complete")
        self.status_badge.setStyleSheet(
            f"background: {Palette.pale_green}; color: {Palette.green}; "
            f"border: 1px solid {Palette.green}; border-radius: 12px; "
            "padding: 5px 12px; font-weight: 700;"
        )
        self.status_bar.showMessage("Evidence search complete — results are ready")

    def _set_busy(self, label: str) -> None:
        self.run_progress.hide()
        is_search = label in {"Searching", "Browser lookups"}
        self._operation_active = True
        if not is_search:
            self._search_started_at = None
        self._search_pause_requested = False
        self._search_stop_requested = False
        self.status_badge.setText(label)
        self.run_status_strip.set_snapshot(
            RunSnapshot(
                phase=RunPhase.RUNNING if is_search else RunPhase.LOADING,
                action=label,
                started_at=datetime.now(),
            )
        )
        self.status_badge.setText(label)
        self.status_badge.setStyleSheet(
            f"background: {Palette.pale_blue}; color: {Palette.blue}; "
            f"border: 1px solid {Palette.blue}; border-radius: 12px; "
            "padding: 5px 12px; font-weight: 700;"
        )
        self.activity_progress.show()
        self.process_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.priority_search_btn.setEnabled(False)
        self.retry_selected_btn.setEnabled(False)
        self.remaining_search_btn.setEnabled(False)
        self.pause_search_btn.setText("Pause")
        self.pause_search_btn.setEnabled(is_search)
        self.stop_search_btn.setText("Stopp")
        self.stop_search_btn.setEnabled(is_search)
        self.browser_signin_btn.setEnabled(False)
        self.check_sessions_btn.setEnabled(False)
        self.browser_review_btn.setEnabled(False)
        self.rewrite_btn.setEnabled(False)
        self.patient_excel_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.retry_report_saves_button.setEnabled(False)
        self._set_analysis_controls_enabled(False)
        self._update_source_access_controls()
        self.status_bar.showMessage(label)
        if is_search:
            self._switch_page(1)
            self.database_scroll.verticalScrollBar().setValue(0)

    def _set_ready(self) -> None:
        self._operation_active = False
        self._active_sources.clear()
        self._search_pending_pairs.clear()
        self._queue_databases.clear()
        self.run_status_strip.set_snapshot(RunSnapshot())
        self.status_badge.setText("Klar")
        self.status_badge.setStyleSheet("")
        self.activity_progress.hide()
        self._update_process_state()
        self.search_btn.setEnabled(self.result is not None)
        self._search_pause_requested = False
        self._search_stop_requested = False
        self.pause_search_btn.setText("Pause")
        self.pause_search_btn.setEnabled(False)
        self.stop_search_btn.setText("Stopp")
        self.stop_search_btn.setEnabled(False)
        self.browser_signin_btn.setEnabled(True)
        self.check_sessions_btn.setEnabled(True)
        self.browser_review_btn.setEnabled(self.result is not None)
        self.rewrite_btn.setEnabled(self.result is not None)
        self.patient_excel_btn.setEnabled(self.result is not None)
        self.resume_btn.setEnabled(True)
        self.retry_report_saves_button.setEnabled(True)
        self._set_analysis_controls_enabled(True)
        self._update_source_access_controls()
        self.continue_evidence_btn.setEnabled(self.result is not None)
        self._update_evidence_summary()
        self._refresh_import_step()
        self.status_bar.showMessage("Klar", 3000)

    def _update_process_state(self) -> None:
        self._refresh_import_step()

    def _show_patient_progress(self) -> None:
        if self._operation_active:
            self.database_scroll.ensureWidgetVisible(self.status_matrix, 0, 12)

    def _set_analysis_controls_enabled(self, enabled: bool) -> None:
        for control in (
            self.input_edit, self.output_edit, self.input_browse_btn, self.output_browse_btn,
            self.hide_excluded, self.file_drop, self.validate_btn, self.restore_recent_button,
            self.dismiss_recent_button, self.included_only_check, self.browser_database_combo,
            self.new_analysis_btn, self.resume_analysis_btn,
            *self.db_checks.values(),
        ):
            control.setEnabled(enabled)
        self.load_selection_btn.setEnabled(enabled and self.result is not None)
        self.run_date.setEnabled(enabled and not (
            sequencing_date_from_path(Path(self.input_edit.text()))
            or sequencing_date_from_path(Path(self.output_edit.text()))
        ))
        self.tabs.widget(2).setEnabled(enabled)
        self._refresh_import_step()

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{timestamp}] {message}")
        if self._operation_active and self._search_started_at is not None and hasattr(self, "latest_action_label"):
            self.latest_action_label.setText(f"{timestamp} · {message[:220]}")
            self.latest_action_label.setToolTip(message)
        if self.run_journal is not None and not self.run_journal.record(message):
            if not self._run_log_warning_shown:
                self._run_log_warning_shown = True
                self.log.appendPlainText(
                    f"[{timestamp}] Full run log could not be written: "
                    f"{self.run_journal.last_error}"
                )
        self.status_bar.showMessage(message, 5000)

    def _start_run_journal(self, output_directory: Path, run_mode: str) -> None:
        if self.run_journal is not None:
            self.run_journal.close()
        self.run_journal = RunJournal.start(
            Path(output_directory) / "vpm_run_logs",
            run_mode=run_mode,
            app_version=__version__,
        )
        self._run_log_warning_shown = False
        self.open_log_folder_btn.setEnabled(not bool(self.run_journal.last_error))

    def _open_run_log_folder(self) -> None:
        if self.run_journal is None or self.run_journal.last_error:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_journal.directory)))

    def _search_elapsed_text(self) -> str:
        if self._search_started_at is None:
            return "0s"
        seconds = max(0, round(time.monotonic() - self._search_started_at))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}h {minutes:02d}m {seconds:02d}s"
        if minutes:
            return f"{minutes:d}m {seconds:02d}s"
        return f"{seconds:d}s"

    def _table_text(self, table: QTableWidget, row: int, col: int) -> str:
        item = table.item(row, col)
        return item.text().strip() if item else ""

    def _apply_style(self) -> None:
        self.setStyleSheet(
            application_stylesheet()
            + f"""
            QMainWindow, QWidget {{
                background: {Palette.app_bg};
                color: {Palette.ink};
                font-family: "Segoe UI";
                font-size: 13px;
            }}
            QLabel {{
                background: transparent;
            }}
            QPushButton#ImportModeButton:checked {{
                background: {Palette.pale_blue};
                color: {Palette.blue};
                border: 2px solid {Palette.blue};
            }}
            QWidget#AppRoot, QWidget#ContentShell {{
                background: {Palette.app_bg};
            }}
            QFrame#Sidebar {{
                background: #08283A;
                border: none;
            }}
            QLabel#BrandMark {{
                background: transparent;
                border: none;
            }}
            QLabel#BrandTitle {{
                background: transparent;
                color: white;
                font-size: 17px;
                font-weight: 750;
                padding-top: 4px;
            }}
            QLabel#SidebarEyebrow {{
                background: transparent;
                color: #8FC5DD;
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 1px;
                padding: 4px 8px;
            }}
            QPushButton#SidebarButton {{
                background: transparent;
                color: #DCECF2;
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 9px 10px;
                text-align: left;
                font-weight: 600;
            }}
            QPushButton#SidebarButton:hover {{
                background: #103E52;
                color: white;
                border-color: #28586A;
            }}
            QPushButton#SidebarButton:checked {{
                background: #0C7696;
                color: white;
                border-color: #49B4C6;
            }}
            QPushButton#SidebarButton:focus {{
                border: 2px solid #7DD3FC;
            }}
            QLabel#PageEyebrow {{
                color: {Palette.blue};
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 1px;
            }}
            QLabel#PageTitle {{
                color: {Palette.navy};
                font-size: 22px;
                font-weight: 750;
            }}
            QLabel#PageSubtitle {{
                color: {Palette.muted};
                font-size: 12px;
            }}
            QStackedWidget#WorkspacePages {{
                background: transparent;
                border: none;
            }}
            QGroupBox {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 9px;
                margin-top: 14px;
                padding: 14px 12px 12px 12px;
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
                border-radius: 6px;
                padding: 7px;
                selection-background-color: {Palette.pale_blue};
                selection-color: {Palette.navy};
            }}
            QLineEdit, QDateEdit, QComboBox, QSpinBox {{
                min-height: 28px;
            }}
            QLineEdit:focus, QDateEdit:focus, QPlainTextEdit:focus,
            QTableWidget:focus, QComboBox:focus, QSpinBox:focus {{
                border: 1px solid {Palette.blue};
            }}
            QPushButton {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 6px;
                padding: 9px 15px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border-color: {Palette.blue};
                background: {Palette.pale_blue};
            }}
            QPushButton:disabled {{
                color: #8293A1;
                background: #E7EEF2;
                border-color: #D2DEE5;
            }}
            QPushButton#PrimaryButton {{
                background: {Palette.blue};
                color: white;
                border-color: {Palette.blue};
            }}
            QPushButton#PrimaryButton:hover {{
                background: #076B8C;
            }}
            QPushButton#OutlineButton {{
                background: {Palette.pale_blue};
                color: {Palette.navy};
                border-color: {Palette.blue};
            }}
            QPushButton#StopButton {{
                min-height: 24px;
                background: {Palette.panel};
                color: {Palette.red};
                border: 1px solid {Palette.red};
            }}
            QPushButton#StopButton:hover {{
                background: {Palette.pale_red};
                border-color: {Palette.red};
            }}
            QPushButton#PauseButton {{
                min-height: 24px;
                background: {Palette.pale_yellow};
                color: #714600;
                border: 1px solid #D7AA4B;
            }}
            QPushButton#PauseButton:hover {{
                background: #FFE9A8;
                border-color: #B77A13;
            }}
            QPushButton#ReportButton {{
                background: {Palette.green};
                color: white;
                border-color: {Palette.green};
            }}
            QPushButton#ReportButton:hover {{
                background: #3F724A;
            }}
            QPushButton#PrimaryButton:disabled, QPushButton#ReportButton:disabled,
            QPushButton#OutlineButton:disabled, QPushButton#PauseButton:disabled,
            QPushButton#StopButton:disabled {{
                background: #E7EEF2;
                color: #536977;
                border: 1px solid #C6D3DB;
            }}
            QTabBar::tab {{ padding: 8px 12px; background: #E7EEF2; color: #163445; }}
            QTabBar::tab:selected {{ background: white; border-top: 2px solid #087EA4; }}
            QFrame#MetricCard {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 8px;
            }}
            QFrame#ToolbarCard, QFrame#RunProgressCard,
            QFrame#EvidenceCommand {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 9px;
            }}
            QFrame#EvidenceCommand {{
                background: #EEF7F8;
                border-left: 4px solid {Palette.blue};
            }}
            QLabel#SectionTitle, QLabel#RunProgressTitle {{
                color: {Palette.navy};
                font-size: 14px;
                font-weight: 750;
            }}
            QLabel#RunProgressCount {{
                color: {Palette.blue};
                font-weight: 700;
            }}
            QCheckBox#SourceCard {{
                background: #F8FBFC;
                color: {Palette.navy};
                border: 1px solid {Palette.border};
                border-radius: 7px;
                padding: 9px 10px;
                font-weight: 650;
            }}
            QWidget#SourceTile {{
                background: transparent;
            }}
            QFrame#AnalysisFileDrop {{
                background: {Palette.pale_blue};
                border: 1px dashed {Palette.blue};
                border-radius: 8px;
            }}
            QCheckBox#SourceCard:hover {{
                background: {Palette.pale_blue};
                border-color: {Palette.cyan};
            }}
            QLabel#SettingsColumnHeader {{
                color: {Palette.muted};
                font-size: 11px;
                font-weight: 700;
                padding-bottom: 3px;
            }}
            QLabel#NotRequired {{
                color: #8094A0;
                background: #F1F5F7;
                border-radius: 6px;
                padding: 8px;
            }}
            QLabel#WebAccess {{
                color: #0A6570;
                background: #E7F4F3;
                border-radius: 6px;
                padding: 8px;
                font-weight: 650;
            }}
            QLabel#HelperText {{
                color: {Palette.muted};
                font-size: 12px;
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
                color: {Palette.navy};
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
            QHeaderView::section {{
                background: {Palette.pale_blue};
                color: {Palette.ink};
                padding: 8px;
                border: none;
                border-bottom: 1px solid {Palette.border};
                font-weight: 700;
            }}
            QTableWidget {{
                gridline-color: #E4EBF0;
                alternate-background-color: #F7FAFC;
            }}
            QTableWidget#PatientStatusMatrix::item:selected {{
                background: #075B79;
                color: white;
                font-weight: 700;
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
            QProgressBar#RunProgressBar {{
                background: #DCE9F0;
                border: none;
                border-radius: 4px;
                min-height: 8px;
                max-height: 8px;
            }}
            QProgressBar#RunProgressBar::chunk {{
                background: {Palette.green};
                border-radius: 4px;
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
    icon_path = Path(__file__).resolve().parents[1] / "assets" / "vpm-tolkning-icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
