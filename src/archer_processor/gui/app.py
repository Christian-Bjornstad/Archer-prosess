from __future__ import annotations

import sys
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QDate, QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QButtonGroup,
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
    QSizePolicy,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from archer_processor.core import DatabaseEvidence, FilterEngine, ProcessingResult, VariantProcessor, default_artifact_rules, production_rules
from archer_processor.core.highlights import priority_warning, variant_highlight
from archer_processor.io import ArcherTsvReader
from archer_processor.reports import (
    ExcelReportWriter,
    PatientExcelReportWriter,
    PatientReportCoordinator,
    PatientReportOutcome,
)
from archer_processor.services import (
    AppSettings,
    BROWSER_DATABASES,
    BrowserReviewCancelled,
    BrowserReviewService,
    DatabaseSearchService,
    ProcessedWorkbookLoader,
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
from archer_processor.gui.widgets.activity_timeline import (
    ActivityTimeline,
    CurrentActivityPanel,
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
            filter_engine = FilterEngine(production_rules(self.settings.artifact_rules))
            processor = VariantProcessor(filter_engine=filter_engine)
            result = processor.process(self.input_path, self.run_date, self.output_path)
            self.status.emit("Writing review workbook")
            ExcelReportWriter().write(result, self.output_path, hide_excluded=self.hide_excluded)
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
                    production_rules(self.settings.artifact_rules)
                ),
            ).load(self.workbook_path, progress=self.progress.emit)
            self.finished.emit(self.workbook_path, state)
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


def _merge_evidence_results(target: dict, incoming: dict) -> None:
    for key, new_items in incoming.items():
        by_database = {item.database: item for item in target.get(key, [])}
        by_database.update({item.database: item for item in new_items})
        target[key] = list(by_database.values())


def _completed_evidence_sources(
    evidence: dict[str, list[DatabaseEvidence]],
) -> set[tuple[str, str]]:
    completed: set[tuple[str, str]] = set()
    for key, items in evidence.items():
        for item in items:
            if is_completed_evidence(item):
                completed.add((key, item.database))
    return completed


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
            and not is_completed_evidence(by_database[database])
            for database in databases
        ):
            failed.append(variant)
    return failed


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

    def run(self) -> None:
        try:
            api_service = DatabaseSearchService(self.settings)
            browser_service = self._browser_service()
            for database, status in api_service.database_diagnostics(self.api_databases).items():
                self.status.emit(f"{database}: {status}")
            for database in self.browser_databases:
                self.status.emit(f"{database}: website lookup in Microsoft Edge")
            patients = _variants_grouped_by_patient(self.variants)
            all_evidence: dict[str, list[DatabaseEvidence]] = {}
            coordinator = (
                PatientReportCoordinator(
                    self.result, self.report_variants, self.existing_evidence
                )
                if self.result is not None
                else None
            )
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
                    _merge_evidence_results(patient_evidence, database_evidence)
                    self.patient_finished.emit(database_evidence)

                if self.browser_databases:
                    if patient_index > 1:
                        self._wait(
                            f"{prefix}: website safety buffer before signed-in sources"
                        )
                    browser_evidence = browser_service.search_variants(
                        patient_variants,
                        self.browser_databases,
                        self.artifact_root / f"patient-{original_patient_index:03d}",
                        progress=lambda message, p=prefix: self.status.emit(f"{p}: {message}"),
                        completed_sources=self.completed_sources,
                        checkpoint=self.patient_finished.emit,
                        prior_evidence=self.existing_evidence,
                    )
                    _merge_evidence_results(patient_evidence, browser_evidence)

                _merge_evidence_results(all_evidence, patient_evidence)
                if coordinator is not None:
                    coordinator.merge(patient_evidence)
                self.status.emit(f"{prefix}: complete")
                self.progress.emit(
                    patient_index,
                    len(patients),
                    f"Completed {patient_id}",
                )
            if coordinator is not None:
                for outcome in coordinator.reconcile():
                    self.report_outcome.emit(outcome)
            self.finished.emit(all_evidence)
        except BrowserReviewCancelled:
            self.cancelled.emit()
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
            stop_requested=lambda: QThread.currentThread().isInterruptionRequested(),
            pause_wait=self._wait_if_paused,
            browser_background=self.settings.browser_background,
        )

    def request_pause(self) -> None:
        self.pause_control.request_pause()

    def resume_search(self) -> None:
        self.pause_control.resume()

    def _wait_if_paused(self) -> None:
        self.pause_control.wait(
            stop_requested=lambda: QThread.currentThread().isInterruptionRequested(),
            pause_changed=self.paused.emit,
        )

    def _check_cancelled(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
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


class BrowserReviewWorker(QObject):
    finished = pyqtSignal(object)
    cancelled = pyqtSignal()
    patient_finished = pyqtSignal(object)
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
                stop_requested=lambda: QThread.currentThread().isInterruptionRequested(),
                pause_wait=self._wait_if_paused,
                browser_background=self.settings.browser_background,
            )
            all_evidence: dict[str, list[DatabaseEvidence]] = {}
            patients = _variants_grouped_by_patient(self.variants)
            coordinator = (
                PatientReportCoordinator(
                    self.result, self.report_variants, self.existing_evidence
                )
                if self.result is not None
                else None
            )
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
                patient_evidence = self._search_patient_with_retries(
                    service,
                    patient_id,
                    patient_variants,
                    original_patient_index,
                    prefix,
                )
                _merge_evidence_results(all_evidence, patient_evidence)
                if coordinator is not None:
                    coordinator.merge(patient_evidence)
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
            if coordinator is not None:
                for outcome in coordinator.reconcile():
                    self.report_outcome.emit(outcome)
            final_retry_evidence = self._final_failed_pass(all_evidence, patients)
            if final_retry_evidence:
                _merge_evidence_results(all_evidence, final_retry_evidence)
                _merge_evidence_results(self._pass_evidence, final_retry_evidence)
                self.patient_finished.emit(final_retry_evidence)
            if coordinator is not None:
                for outcome in coordinator.reconcile():
                    self.report_outcome.emit(outcome)
            self.finished.emit(all_evidence)
        except BrowserReviewCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))

    def _search_patient_with_retries(
        self,
        service: BrowserReviewService,
        patient_id: str,
        patient_variants: list,
        original_patient_index: int,
        prefix: str,
    ) -> dict[str, list[DatabaseEvidence]]:
        """Search one patient, then retry failed lookups once before moving on.

        The first pass collects everything; if any variant/source pair ends in a
        retryable state (site down, timeout, lost session), a single immediate
        retry pass runs for just those lookups. Anything still failing afterwards
        is left for the batch-end pass or the manual rerun button.
        """
        patient_evidence = self._run_search_pass(
            service, patient_id, patient_variants, original_patient_index, prefix
        )
        _merge_evidence_results(self._pass_evidence, patient_evidence)
        failed = _failed_search_variants(
            patient_variants, patient_evidence, self.databases
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
        )
        _merge_evidence_results(patient_evidence, retry_evidence)
        _merge_evidence_results(self._pass_evidence, retry_evidence)
        still_failed = _failed_search_variants(
            failed, patient_evidence, self.databases
        )
        if still_failed:
            self.status.emit(
                f"{prefix}: {len(still_failed)} lookup(s) still failing after "
                "retry; a final pass runs after the last patient"
            )
        return patient_evidence

    def _run_search_pass(
        self,
        service: BrowserReviewService,
        patient_id: str,
        variants: list,
        original_patient_index: int,
        prefix: str,
    ) -> dict[str, list[DatabaseEvidence]]:
        completed_sources = self.completed_sources | _completed_evidence_sources(
            self._pass_evidence
        )
        prior_evidence = {
            key: list(items) for key, items in self.existing_evidence.items()
        }
        _merge_evidence_results(prior_evidence, self._pass_evidence)
        return service.search_variants(
            variants,
            self.databases,
            self.artifact_root / f"patient-{original_patient_index:03d}",
            progress=lambda message, p=prefix: self.status.emit(f"{p}: {message}"),
            activity=lambda database, message, patient=patient_id, pass_variants=variants: self.activity.emit(
                RunActivity(
                    occurred_at=datetime.now(),
                    patient_id=patient,
                    database=database,
                    variant_label=(pass_variants[0].display_name if len(pass_variants) == 1 else f"{len(pass_variants)} variants"),
                    action=message,
                    message=message,
                )
            ),
            completed_sources=completed_sources,
            checkpoint=self.patient_finished.emit,
            prior_evidence=prior_evidence,
        )

    def _final_failed_pass(
        self,
        all_evidence: dict[str, list[DatabaseEvidence]],
        patients: list[tuple[str, list]],
    ) -> dict[str, list[DatabaseEvidence]]:
        """One last pass over every still-failed lookup once the batch completes."""
        all_variants = [
            variant for _, patient_variants in patients for variant in patient_variants
        ]
        evidence_snapshot: dict[str, list[DatabaseEvidence]] = {
            key: list(items) for key, items in all_evidence.items()
        }
        for key, items in self._pass_evidence.items():
            _merge_evidence_results(evidence_snapshot, {key: list(items)})
        failed = _failed_search_variants(
            all_variants, evidence_snapshot, self.databases
        )
        if not failed:
            return {}
        self.status.emit(
            f"Batch complete: running one final pass for {len(failed)} "
            "still-failed lookup(s)"
        )
        service = self._build_service()
        final_evidence: dict[str, list[DatabaseEvidence]] = {
            BrowserReviewService.variant_key(variant): [] for variant in failed
        }
        for patient_id, patient_variants in _variants_grouped_by_patient(failed):
            original_index = self.patient_indexes.get(patient_id, 1)
            pass_prefix = f"Patient {original_index} ({patient_id}) (final pass)"
            _merge_evidence_results(
                final_evidence,
                self._run_search_pass(
                    service, patient_id, patient_variants, original_index, pass_prefix
                ),
            )
        still_failed = _failed_search_variants(
            failed, final_evidence, self.databases
        )
        if still_failed:
            self.status.emit(
                f"{len(still_failed)} lookup(s) remain unresolved; use 'Rerun "
                "Failed Sources' to try again later."
            )
        return final_evidence

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
            stop_requested=lambda: QThread.currentThread().isInterruptionRequested(),
            pause_wait=self._wait_if_paused,
            browser_background=self.settings.browser_background,
        )

    def request_pause(self) -> None:
        self.pause_control.request_pause()

    def resume_search(self) -> None:
        self.pause_control.resume()

    def _wait_if_paused(self) -> None:
        self.pause_control.wait(
            stop_requested=lambda: QThread.currentThread().isInterruptionRequested(),
            pause_changed=self.paused.emit,
        )

    def _check_cancelled(self) -> None:
        if QThread.currentThread().isInterruptionRequested():
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
        self.setObjectName("RunProgressCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        self.title = QLabel("Evidence search")
        self.title.setObjectName("RunProgressTitle")
        self.count = QLabel("0 / 0 patients")
        self.count.setObjectName("RunProgressCount")
        heading.addWidget(self.title)
        heading.addStretch()
        heading.addWidget(self.count)
        self.detail = QLabel("Preparing patient queue")
        self.detail.setObjectName("HelperText")
        self.bar = QProgressBar()
        self.bar.setObjectName("RunProgressBar")
        self.bar.setTextVisible(False)
        layout.addLayout(heading)
        layout.addWidget(self.detail)
        layout.addWidget(self.bar)
        self.hide()

    def update_progress(self, current: int, total: int, detail: str) -> None:
        safe_total = max(1, total)
        self.bar.setRange(0, safe_total)
        self.bar.setValue(min(max(0, current), safe_total))
        self.count.setText(f"{current} / {total} patients")
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
        self.workbook_load_thread: QThread | None = None
        self.report_retry_thread: QThread | None = None
        self.database_thread: QThread | None = None
        self.browser_thread: QThread | None = None
        self.processing_worker: ProcessingWorker | None = None
        self.workbook_load_worker: ProcessedWorkbookWorker | None = None
        self.database_worker: DatabaseWorker | None = None
        self.browser_worker: BrowserLoginWorker | BrowserReviewWorker | None = None
        self._search_pause_requested = False
        self._search_stop_requested = False
        self._search_started_at: float | None = None
        self.workbook_write_pending = False
        self._workbook_lock_warning_shown = False
        self.setWindowTitle("Myolid Tolkning")
        self.app_icon_path = (
            Path(__file__).resolve().parents[1] / "assets" / "vpm-tolkning-icon.png"
        )
        if self.app_icon_path.exists():
            self.setWindowIcon(QIcon(str(self.app_icon_path)))
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)
        self._build_ui()
        self._update_evidence_summary()
        self._apply_style()

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
        layout.setContentsMargins(24, 20, 24, 14)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        self.page_eyebrow = QLabel("VPM INTERPRETATION  /  IMPORT")
        self.page_eyebrow.setObjectName("PageEyebrow")
        self.page_title = QLabel("Analysis workspace")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel(
            "Start from a variant dataset or resume a processed review workbook"
        )
        self.page_subtitle.setObjectName("PageSubtitle")
        title_box.addWidget(self.page_eyebrow)
        title_box.addWidget(self.page_title)
        title_box.addWidget(self.page_subtitle)
        header.addLayout(title_box)
        header.addStretch()
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
        layout.addWidget(self.run_progress)

        self.tabs = QStackedWidget()
        self.tabs.setObjectName("WorkspacePages")
        self.tabs.addWidget(self._processing_tab())
        self.tabs.addWidget(self._review_tab())
        self.tabs.addWidget(self._database_tab())
        self.tabs.addWidget(self._settings_tab_v2())
        layout.addWidget(self.tabs, 1)
        shell.addWidget(content, 1)

        for control in [
            *root.findChildren(QPushButton),
            *root.findChildren(QCheckBox),
        ]:
            control.setCursor(Qt.CursorShape.PointingHandCursor)

        self.setCentralWidget(root)
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _switch_page(self, index: int) -> None:
        pages = [
            ("IMPORT", "Analysis workspace", "Start from a variant dataset or resume a processed review workbook"),
            ("VARIANTS", "Variant review", "Filter, inspect, and prioritise calls for evidence research"),
            ("EVIDENCE", "Evidence search", "Research selected variants across clinical and cancer databases"),
            ("SETTINGS", "Configuration", "Manage local files, provider access, and search safeguards"),
        ]
        if not 0 <= index < len(pages):
            return
        self.tabs.setCurrentIndex(index)
        self.navigation.set_current(index)
        eyebrow, title, subtitle = pages[index]
        self.page_eyebrow.setText(f"VPM INTERPRETATION  /  {eyebrow}")
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)

    def _processing_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)

        files = QGroupBox("Analysis input")
        grid = QGridLayout(files)
        grid.setColumnStretch(1, 1)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Select the filtered variant TSV")
        self.input_edit.textChanged.connect(self._update_process_state)
        input_btn = QPushButton("Browse")
        input_btn.clicked.connect(self._browse_input)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Clinical review workbook (.xlsx)")
        self.output_edit.textChanged.connect(self._update_process_state)
        output_btn = QPushButton("Browse")
        output_btn.clicked.connect(self._browse_output)
        self.run_date = QDateEdit()
        self.run_date.setCalendarPopup(True)
        self.run_date.setDisplayFormat("yyyy-MM-dd")
        self.run_date.setDate(QDate.currentDate())
        self.hide_excluded = QCheckBox("Hide excluded rows in workbook")
        self.hide_excluded.setChecked(False)
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
        self.process_btn = QPushButton("Create Review Workbook")
        self.process_btn.setObjectName("PrimaryButton")
        self.process_btn.setEnabled(False)
        self.process_btn.clicked.connect(self._start_processing)
        actions.addStretch()
        actions.addWidget(self.validate_btn)
        actions.addWidget(self.process_btn)
        layout.addLayout(actions)

        self.recent_analysis_panel = QFrame()
        self.recent_analysis_panel.setObjectName("RecentAnalysisPanel")
        recent_layout = QHBoxLayout(self.recent_analysis_panel)
        recent_layout.setContentsMargins(14, 12, 14, 12)
        recent_copy = QVBoxLayout()
        recent_title = QLabel("Continue recent analysis")
        recent_title.setObjectName("SectionTitle")
        self.recent_analysis_name = QLabel()
        self.recent_analysis_name.setObjectName("FieldLabel")
        self.recent_analysis_detail = QLabel()
        self.recent_analysis_detail.setObjectName("HelperText")
        recent_copy.addWidget(recent_title)
        recent_copy.addWidget(self.recent_analysis_name)
        recent_copy.addWidget(self.recent_analysis_detail)
        recent_layout.addLayout(recent_copy, 1)
        self.restore_recent_button = QPushButton("Restore analysis")
        self.restore_recent_button.setObjectName("PrimaryButton")
        self.restore_recent_button.setMinimumHeight(44)
        self.dismiss_recent_button = QPushButton("Dismiss")
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
        layout.addWidget(self.recent_analysis_panel)

        resume = QGroupBox("Continue previous analysis")
        resume_layout = QVBoxLayout(resume)
        resume_layout.setSpacing(8)
        resume_help = QLabel(
            "Restore variants, X selections, and collected evidence from a processed VPM workbook."
        )
        resume_help.setObjectName("HelperText")
        resume_help.setWordWrap(True)
        resume_row = QHBoxLayout()
        self.resume_edit = QLineEdit()
        self.resume_edit.setReadOnly(True)
        self.resume_edit.setPlaceholderText("No processed workbook loaded")
        self.resume_edit.setAccessibleName("Processed VPM workbook")
        self.resume_btn = QPushButton("Open Processed Workbook")
        self.resume_btn.setObjectName("OutlineButton")
        self.resume_btn.setMinimumHeight(44)
        self.resume_btn.setToolTip(
            "Resume a workbook created by Myolid Tolkning without reprocessing the TSV."
        )
        self.resume_btn.clicked.connect(self._browse_processed_workbook)
        resume_row.addWidget(self.resume_edit, 1)
        resume_row.addWidget(self.resume_btn)
        self.resume_status = QLabel("Ready to restore an existing review session.")
        self.resume_status.setObjectName("HelperText")
        resume_layout.addWidget(resume_help)
        resume_layout.addLayout(resume_row)
        resume_layout.addWidget(self.resume_status)
        layout.addWidget(resume)

        activity = QGroupBox("Activity")
        activity.setMaximumHeight(190)
        activity_layout = QVBoxLayout(activity)
        activity_help = QLabel("Validation and processing messages for the current analysis.")
        activity_help.setObjectName("HelperText")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setPlaceholderText("No activity yet. Select a variant TSV to begin.")
        activity_layout.addWidget(activity_help)
        activity_layout.addWidget(self.log, 1)
        layout.addWidget(activity)
        layout.addStretch()
        return page

    def _review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.variant_toolbar = QFrame()
        self.variant_toolbar.setObjectName("VariantToolbar")
        toolbar_layout = QHBoxLayout(self.variant_toolbar)
        toolbar_layout.setContentsMargins(14, 10, 14, 10)
        self.review_filter_edit = QLineEdit()
        self.review_filter_edit.setPlaceholderText("Filter sample, gene, HGVS, or warning")
        self.review_filter_edit.setClearButtonEnabled(True)
        self.review_filter_edit.textChanged.connect(self._apply_review_filters)
        self.review_decision_combo = QComboBox()
        self.review_decision_combo.addItems(["All decisions", "Included", "Excluded"])
        self.review_decision_combo.currentIndexChanged.connect(self._apply_review_filters)
        self.review_count_label = QLabel("No variants loaded")
        self.review_count_label.setObjectName("HelperText")
        self.variant_counters = QLabel("0 total · 0 included · 0 excluded")
        self.variant_counters.setObjectName("VariantCounters")
        toolbar_layout.addWidget(QLabel("Find variants"))
        toolbar_layout.addWidget(self.review_filter_edit, 1)
        toolbar_layout.addWidget(self.review_decision_combo)
        toolbar_layout.addWidget(self.variant_counters)
        toolbar_layout.addWidget(self.review_count_label)
        layout.addWidget(self.variant_toolbar)
        self.variant_table = QTableWidget(0, 7)
        self.variant_table.setHorizontalHeaderLabels(
            ["Sample", "Gene", "HGVSc", "AF", "Depth", "Decision", "Warnings"]
        )
        self.variant_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.variant_table.setAlternatingRowColors(True)
        self.variant_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.variant_table.setMinimumHeight(420)
        layout.addWidget(self.variant_table, 1)
        self.variant_empty_state = QLabel(
            "No variants match the current filters. Clear filters to restore all rows."
        )
        self.variant_empty_state.setObjectName("VariantEmptyState")
        self.variant_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.variant_empty_state.hide()
        layout.addWidget(self.variant_empty_state)
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

        command = QFrame()
        command.setObjectName("EvidenceCommand")
        command_layout = QHBoxLayout(command)
        command_layout.setContentsMargins(16, 13, 16, 13)
        command_copy = QVBoxLayout()
        command_title = QLabel("Research queue")
        command_title.setObjectName("SectionTitle")
        self.evidence_summary = QLabel("Choose sources to prepare a search")
        self.evidence_summary.setObjectName("HelperText")
        self.evidence_summary.setWordWrap(True)
        self.evidence_summary.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        command_copy.addWidget(command_title)
        command_copy.addWidget(self.evidence_summary)
        command_layout.addLayout(command_copy, 1)
        self.search_btn = QPushButton("Run Evidence Search")
        self.search_btn.setObjectName("PrimaryButton")
        self.search_btn.setFixedWidth(190)
        self.search_btn.setMinimumHeight(44)
        self.search_btn.setEnabled(False)
        self.search_btn.clicked.connect(self._start_database_search)
        command_layout.addWidget(self.search_btn)
        self.pause_search_btn = QPushButton("Pause Search")
        self.pause_search_btn.setObjectName("PauseButton")
        self.pause_search_btn.setFixedWidth(124)
        self.pause_search_btn.setMinimumHeight(44)
        self.pause_search_btn.setEnabled(False)
        self.pause_search_btn.setAccessibleName("Pause or resume evidence search")
        self.pause_search_btn.setToolTip(
            "Pause at the next safe checkpoint, then resume the same queue without repeating completed work."
        )
        self.pause_search_btn.clicked.connect(self._toggle_search_pause)
        command_layout.addWidget(self.pause_search_btn)
        self.stop_search_btn = QPushButton("Stop Search")
        self.stop_search_btn.setObjectName("StopButton")
        self.stop_search_btn.setFixedWidth(118)
        self.stop_search_btn.setMinimumHeight(44)
        self.stop_search_btn.setEnabled(False)
        self.stop_search_btn.setAccessibleName("Stop evidence search")
        self.stop_search_btn.setToolTip(
            "Safely stop after the current browser action. Evidence already collected is kept."
        )
        self.stop_search_btn.clicked.connect(self._stop_evidence_search)
        command_layout.addWidget(self.stop_search_btn)
        layout.addWidget(command)

        self.current_activity = CurrentActivityPanel()
        layout.addWidget(self.current_activity)

        checks = QGroupBox("Evidence sources")
        checks.setMinimumHeight(250)
        grid = QGridLayout(checks)
        grid.setHorizontalSpacing(9)
        grid.setVerticalSpacing(6)
        source_copy = {
            "MTBP": "Report synthesis",
            "Franklin": "Variant prediction",
            "ClinVar": "Clinical classification",
            "OncoKB": "Oncology knowledge",
            "COSMIC": "Tumour evidence",
        }
        self.db_checks: dict[str, QCheckBox] = {}
        for index, database in enumerate(self.databases):
            check = QCheckBox(f"{database}\n{source_copy[database]}")
            check.setObjectName("SourceCard")
            check.setMinimumHeight(60)
            check.setChecked(database in self.settings.enabled_databases)
            if database in BROWSER_DATABASES:
                check.setProperty("loginSource", True)
                check.setToolTip("Uses the saved signed-in browser session.")
            self.db_checks[database] = check
            check.stateChanged.connect(self._update_evidence_summary)
            row, column = divmod(index, 2)
            grid.addWidget(check, row, column)
            grid.setColumnStretch(column, 1)
        layout.addWidget(checks)

        status_group = QGroupBox("Patient progress")
        status_layout = QVBoxLayout(status_group)
        self.status_matrix = StatusMatrix(self.databases)
        self.status_matrix.setMinimumHeight(210)
        status_layout.addWidget(self.status_matrix)
        layout.addWidget(status_group)

        timeline_group = QGroupBox("Activity timeline")
        timeline_layout = QVBoxLayout(timeline_group)
        self.activity_timeline = ActivityTimeline()
        self.activity_timeline.setMinimumHeight(160)
        timeline_layout.addWidget(self.activity_timeline)
        layout.addWidget(timeline_group)

        options = QGroupBox("Search scope and browser session")
        options_grid = QGridLayout(options)
        options_grid.setColumnStretch(1, 1)
        options_grid.setHorizontalSpacing(12)
        options_grid.setVerticalSpacing(9)
        scope_label = QLabel("Lookup scope")
        scope_label.setObjectName("FieldLabel")
        options_grid.addWidget(scope_label, 0, 0)
        self.included_only_check = QCheckBox("Included variants only")
        self.included_only_check.setChecked(self.settings.search_included_only)
        self.included_only_check.stateChanged.connect(self._update_evidence_summary)
        self.included_only_check.setToolTip(
            "When enabled, excluded and flagged variants are not sent to any database website."
        )
        options_grid.addWidget(self.included_only_check, 0, 1, 1, 3)
        serial_label = QLabel("Serial patient queue")
        serial_label.setObjectName("FieldLabel")
        options_grid.addWidget(serial_label, 1, 0)
        self.worker_count = QSpinBox()
        self.worker_count.setRange(1, 1)
        self.worker_count.setValue(1)
        self.worker_count.setFixedWidth(72)
        self.worker_count.setToolTip(
            "Patient-centric evidence collection is deliberately serial to avoid bursts of requests."
        )
        options_grid.addWidget(self.worker_count, 1, 1)
        selection_label = QLabel("Reviewed workbook")
        selection_label.setObjectName("FieldLabel")
        options_grid.addWidget(selection_label, 2, 0)
        self.selection_status = QLabel("No skip list loaded")
        self.selection_status.setObjectName("HelperText")
        self.selection_status.setWordWrap(True)
        options_grid.addWidget(self.selection_status, 2, 1, 1, 2)
        self.load_selection_btn = QPushButton("Load Selection Workbook")
        self.load_selection_btn.setFixedWidth(190)
        self.load_selection_btn.setEnabled(False)
        self.load_selection_btn.setToolTip(
            "Load the processed workbook and skip rows marked X on With Artifacts."
        )
        self.load_selection_btn.clicked.connect(self._load_database_selection)
        options_grid.addWidget(self.load_selection_btn, 2, 3)

        browser_label = QLabel("Browser provider")
        browser_label.setObjectName("FieldLabel")
        self.browser_database_combo = QComboBox()
        self.browser_database_combo.addItems(list(BROWSER_DATABASES))
        self.browser_signin_btn = QPushButton("Sign In")
        self.browser_signin_btn.setFixedWidth(90)
        self.browser_signin_btn.setToolTip(
            "Uses credentials stored by Windows Credential Manager, or opens Edge for manual sign-in. The profile is released automatically after success."
        )
        self.browser_signin_btn.clicked.connect(self._start_browser_login)
        self.browser_review_btn = QPushButton("Run Browser Sources")
        self.browser_review_btn.setFixedWidth(170)
        self.browser_review_btn.setObjectName("OutlineButton")
        self.browser_review_btn.setEnabled(False)
        self.browser_review_btn.setToolTip(
            "Runs selected ClinVar, COSMIC, OncoKB, Franklin, and MTBP sources patient-by-patient in visible Edge."
        )
        self.browser_review_btn.clicked.connect(self._start_browser_review)
        self.rerun_failed_btn = QPushButton("Rerun Failed Sources")
        self.rerun_failed_btn.setFixedWidth(170)
        self.rerun_failed_btn.setObjectName("OutlineButton")
        self.rerun_failed_btn.setEnabled(False)
        self.rerun_failed_btn.setToolTip(
            "Re-runs only the variant/source lookups that ended in a retryable state "
            "(website down, timeout, lost session). Safe to press repeatedly."
        )
        self.rerun_failed_btn.clicked.connect(self._rerun_failed_sources)
        options_grid.addWidget(browser_label, 3, 0)
        options_grid.addWidget(self.browser_database_combo, 3, 1)
        options_grid.addWidget(self.browser_signin_btn, 3, 2)
        options_grid.addWidget(self.browser_review_btn, 3, 3)
        options_grid.addWidget(self.rerun_failed_btn, 4, 0)
        self.browser_security_label = QLabel(
            "Local privacy guard: only variant coordinates are sent; patient and sample identifiers remain on this computer."
        )
        self.browser_security_label.setObjectName("SecurityNote")
        self.browser_security_label.setWordWrap(True)
        options_grid.addWidget(self.browser_security_label, 5, 1, 1, 3)
        layout.addWidget(options)

        exports = QGroupBox("Reports")
        exports.setMinimumHeight(105)
        export_layout = QHBoxLayout(exports)
        export_help = QLabel(
            "Generate VPM interpretation workbooks with Oversikt, Vedlegg, variant sheets, evidence links, and screenshots."
        )
        export_help.setObjectName("HelperText")
        export_help.setWordWrap(True)
        self.rewrite_btn = QPushButton("Update Review Workbook")
        self.rewrite_btn.setFixedWidth(185)
        self.rewrite_btn.setEnabled(False)
        self.rewrite_btn.clicked.connect(self._rewrite_workbook)
        self.patient_excel_btn = QPushButton("Create Patient Workbooks")
        self.patient_excel_btn.setFixedWidth(190)
        self.patient_excel_btn.setObjectName("ReportButton")
        self.patient_excel_btn.setEnabled(False)
        self.patient_excel_btn.setToolTip(
            "Creates one image-led Excel evidence report per DIT/patient."
        )
        self.patient_excel_btn.clicked.connect(self._export_patient_excels)
        self.retry_report_saves_button = QPushButton("Retry Pending Saves")
        self.retry_report_saves_button.setObjectName("OutlineButton")
        self.retry_report_saves_button.clicked.connect(self._retry_pending_report_saves)
        self.retry_report_saves_button.hide()
        export_layout.addWidget(export_help, 1)
        export_layout.addWidget(self.rewrite_btn)
        export_layout.addWidget(self.patient_excel_btn)
        export_layout.addWidget(self.retry_report_saves_button)
        layout.addWidget(exports)

        evidence_group = QGroupBox("Evidence matrix")
        evidence_group.setMinimumHeight(330)
        evidence_layout = QVBoxLayout(evidence_group)
        self.evidence_result_summary = QLabel(
            "No evidence collected yet. Run selected sources to populate this table."
        )
        self.evidence_result_summary.setObjectName("HelperText")
        self.evidence_result_summary.setWordWrap(True)
        self.evidence_result_summary.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        evidence_layout.addWidget(self.evidence_result_summary)
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
        self._update_evidence_summary()
        return page

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

        local_group = QGroupBox("Local files")
        local_grid = QGridLayout(local_group)
        local_grid.setColumnStretch(1, 1)
        self.output_dir_edit = QLineEdit(self.settings.default_output_dir)
        dir_btn = QPushButton("Browse")
        dir_btn.clicked.connect(self._browse_output_dir)
        local_grid.addWidget(QLabel("Default report folder"), 0, 0)
        local_grid.addWidget(self.output_dir_edit, 0, 1)
        local_grid.addWidget(dir_btn, 0, 2)
        layout.addWidget(local_group)

        access_group = QGroupBox("Browser access")
        access_grid = QGridLayout(access_group)
        for column in range(1, 4):
            access_grid.setColumnStretch(column, 1)
        for column, title in enumerate(["Provider", "Web access", "Account email", "Password"]):
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
            label = QLabel("Not required")
            label.setObjectName("NotRequired")
            return label

        def web_access(text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("WebAccess")
            return label

        provider_rows = [
            ("ClinVar", web_access("Public website"), not_required(), not_required()),
            ("COSMIC", web_access("Signed-in website"), self.cosmic_email_edit, self.cosmic_password_edit),
            ("OncoKB", web_access("Signed-in website"), self.oncokb_email_edit, self.oncokb_password_edit),
            ("Franklin", web_access("Signed-in website"), self.franklin_email_edit, self.franklin_password_edit),
            ("MTBP", web_access("Signed-in website"), self.mtbp_email_edit, self.mtbp_password_edit),
        ]
        for row, (provider, access, email, password) in enumerate(provider_rows, start=1):
            provider_label = QLabel(provider)
            provider_label.setObjectName("FieldLabel")
            access_grid.addWidget(provider_label, row, 0)
            access_grid.addWidget(access, row, 1)
            access_grid.addWidget(email, row, 2)
            access_grid.addWidget(password, row, 3)
        layout.addWidget(access_group)

        safety_group = QGroupBox("Search pacing")
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
        delay_layout.addWidget(QLabel("Maximum"))
        delay_layout.addWidget(self.browser_delay_max_spin)
        provider_switch_note = QLabel(
            "Fixed 3 seconds when moving from one provider website to the next."
        )
        provider_switch_note.setObjectName("HelperText")
        provider_switch_note.setWordWrap(True)
        delay_layout.addStretch()
        self.mtbp_timeout_spin = QSpinBox()
        self.mtbp_timeout_spin.setRange(5, 60)
        self.mtbp_timeout_spin.setSuffix(" min")
        self.mtbp_timeout_spin.setValue(self.settings.mtbp_timeout_minutes)
        self.browser_background_check = QCheckBox(
            "Keep automated Edge windows minimized"
        )
        self.browser_background_check.setChecked(self.settings.browser_background)
        self.browser_background_check.stateChanged.connect(
            self._update_evidence_summary
        )
        self.browser_background_check.setToolTip(
            "Recommended while you work in other programs. Sign-in windows still open visibly when requested."
        )
        safety_grid.addWidget(QLabel("MTBP cancer type"), 0, 0)
        safety_grid.addWidget(self.mtbp_cancer_type_edit, 0, 1)
        safety_grid.addWidget(QLabel("Between variants"), 1, 0)
        safety_grid.addLayout(delay_layout, 1, 1)
        safety_grid.addWidget(QLabel("Between providers"), 2, 0)
        safety_grid.addWidget(provider_switch_note, 2, 1)
        safety_grid.addWidget(QLabel("MTBP report timeout"), 3, 0)
        safety_grid.addWidget(self.mtbp_timeout_spin, 3, 1)
        safety_grid.addWidget(QLabel("Browser visibility"), 4, 0)
        safety_grid.addWidget(self.browser_background_check, 4, 1)
        layout.addWidget(safety_group)

        artifact_group = QGroupBox("Artifact rules")
        artifact_layout = QVBoxLayout(artifact_group)
        artifact_note = QLabel(
            "Defaults: 36 HGVSc entries from ‘Artefakter DNA Fragmentering v2’. "
            "ASXL1 NM_015338.5:c.1934dup is kept when AF is above 5.5%."
        )
        artifact_note.setObjectName("HelperText")
        artifact_note.setWordWrap(True)
        artifact_layout.addWidget(artifact_note)
        self.artifact_table = QTableWidget(0, 4)
        self.artifact_table.setHorizontalHeaderLabels(
            ["Gene", "HGVSc", "Artifact through AF", "Reason"]
        )
        artifact_header = self.artifact_table.horizontalHeader()
        artifact_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        artifact_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        artifact_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        artifact_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.artifact_table.setMinimumHeight(240)
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
        artifact_layout.addWidget(self.artifact_table)
        artifact_layout.addLayout(artifact_actions)
        layout.addWidget(artifact_group)
        self.settings_groups = [
            local_group,
            access_group,
            safety_group,
            artifact_group,
        ]

        save_row = QHBoxLayout()
        save_btn = QPushButton("Save Configuration")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._save_settings)
        save_row.addStretch()
        save_row.addWidget(save_btn)
        layout.addLayout(save_row)
        layout.addStretch()
        self.settings_scroll.setWidget(settings_content)
        page_layout.addWidget(self.settings_scroll)
        return page


    def _browse_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select variant TSV", "", "TSV files (*.tsv *.txt);;All files (*.*)")
        if path:
            self.input_edit.setText(path)
            output = Path(self.settings.default_output_dir) / f"{Path(path).stem}_VPM_review.xlsx"
            self.output_edit.setText(str(output))

    def _browse_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Workbook", "", "Excel workbook (*.xlsx)")
        if path:
            self.output_edit.setText(path if path.lower().endswith(".xlsx") else f"{path}.xlsx")

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Default Output Folder", self.output_dir_edit.text())
        if path:
            self.output_dir_edit.setText(path)

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
        self.evidence = {}
        self.database_skip_keys = set()
        self.resume_edit.clear()
        self.resume_status.setText("New review workbook created from the selected TSV.")
        self.selection_status.setText("No skip list loaded")
        self._log(f"Complete: {result.total_count} variants, {len(result.included)} included, {len(result.excluded)} excluded")
        self._refresh_metrics()
        self._refresh_variant_table()
        self.search_btn.setEnabled(True)
        self.search_btn.setText("Run Evidence Search")
        self.browser_review_btn.setEnabled(True)
        self.rewrite_btn.setEnabled(True)
        self.patient_excel_btn.setEnabled(True)
        self.load_selection_btn.setEnabled(True)
        if result.output_path is not None:
            self._remember_recent_workbook(result.output_path)
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
        browser_databases = self._selected_browser_databases()
        api_databases: list[str] = []
        completed_sources = _completed_evidence_sources(self.evidence)
        eligible_variants = self._variants_for_search()
        variants = self._pending_variants_for_search(databases)
        if not variants:
            self._show_search_already_complete(databases)
            return
        total_units = len(eligible_variants) * len(databases)
        pending_units = sum(
            (BrowserReviewService.variant_key(variant), database)
            not in completed_sources
            for variant in eligible_variants
            for database in databases
        )
        self._search_started_at = time.monotonic()
        self._set_busy("Searching")
        self.search_btn.setText("Run Evidence Search")
        self._log(
            f"Resume-aware scope: {len(variants)}/{len(eligible_variants)} variant(s), "
            f"{pending_units}/{total_units} source lookup(s) pending"
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
        self.database_thread = thread
        self.database_worker = worker
        thread.start()

    def _database_finished(self, evidence: dict) -> None:
        _merge_evidence_results(self.evidence, evidence)
        self._refresh_evidence_table()
        self._auto_rewrite_workbook()
        self._set_ready()
        self._complete_run_progress("Evidence search complete")
        self.search_btn.setText("Run Evidence Search")
        self._log(
            f"Patient-by-patient evidence search complete ({self._search_elapsed_text()})"
        )

    def _database_patient_finished(self, patient_evidence: dict) -> None:
        _merge_evidence_results(self.evidence, patient_evidence)
        self._refresh_evidence_table()
        self._update_evidence_summary()
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

    def _rerun_failed_sources(self) -> None:
        """Re-run only lookups that ended in a retryable state (e.g. site was down).

        Completed evidence is never repeated; only variant/source pairs whose last
        attempt produced a retryable status (error, timeout, session_lost, ...)
        are re-queued.
        """
        if not self.result:
            return
        if (
            self.browser_thread is not None
            and self.browser_thread.isRunning()
        ) or (
            self.database_thread is not None
            and self.database_thread.isRunning()
        ):
            QMessageBox.information(
                self,
                "Search in progress",
                "An evidence search is already running. Wait for it to finish "
                "before rerunning failed lookups.",
            )
            return
        databases = [
            database
            for database in BROWSER_DATABASES
            if _has_failed_lookups(
                self._variants_for_search(), self.evidence, [database]
            )
        ]
        if not databases:
            QMessageBox.information(
                self,
                "No failed lookups",
                "There are no failed source lookups to rerun. Everything "
                "searched so far is complete.",
            )
            return
        failed_variants = _failed_search_variants(
            self._variants_for_search(), self.evidence, databases
        )
        self._log(
            f"Manual rerun: {len(failed_variants)} failed variant lookup(s) across "
            f"{', '.join(databases)}"
        )
        self._launch_browser_review(databases, variants=failed_variants)

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

    def _pending_variants_for_search(self, databases: list[str]):
        completed_sources = _completed_evidence_sources(self.evidence)
        return [
            variant
            for variant in self._variants_for_search()
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
        self.search_btn.setText("Run Evidence Search")
        self._log(f"Nothing to resume; selected sources already complete: {source_text}")

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
        if self.workbook_load_thread and self.workbook_load_thread.isRunning():
            return
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
        self.workbook_load_worker = worker
        self.workbook_load_thread = thread
        thread.start()

    def _processed_workbook_progress(
        self, current: int, total: int, detail: str
    ) -> None:
        self.run_progress.update_progress(current, total, detail)

    def _processed_workbook_loaded(self, workbook_path: Path, state) -> None:
        self.result = state.result
        self.evidence = state.evidence
        self.database_skip_keys = state.database_skip_keys
        self.workbook_write_pending = False
        self._workbook_lock_warning_shown = False
        self.output_edit.setText(str(workbook_path))
        self.resume_edit.setText(str(workbook_path))
        self.included_only_check.setChecked(False)
        evidence_count = sum(len(items) for items in self.evidence.values())
        searched = self.result.total_count - len(self.database_skip_keys)
        self.selection_status.setText(
            f"{len(self.database_skip_keys)} marked X; {searched} variants will be searched"
        )
        self.resume_status.setText(
            f"Restored {self.result.total_count} variants, "
            f"{len(self.database_skip_keys)} X selection(s), and "
            f"{evidence_count} evidence result(s)."
        )
        self.search_btn.setText(
            "Resume Incomplete Search" if self.evidence else "Run Evidence Search"
        )
        self._refresh_metrics()
        self._refresh_variant_table()
        self._refresh_evidence_table()
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
            "The analysis is ready in Variants and Evidence.",
        )

    def _remember_recent_workbook(self, workbook_path: Path) -> None:
        self.settings.last_processed_workbook = str(workbook_path)
        self.settings.save()
        self.recent_analysis_panel.hide()

    def _processed_workbook_failed(self, message: str) -> None:
        self._set_ready()
        self._log(f"Could not resume processed workbook: {message}")
        QMessageBox.critical(
            self,
            "Processed workbook could not be loaded",
            f"{message}\n\nChoose a workbook created by the current Myolid Tolkning review workflow.",
        )

    def _browser_review_finished(self, browser_evidence: dict) -> None:
        self._merge_browser_evidence(browser_evidence)
        self._refresh_evidence_table()
        self._auto_rewrite_workbook()
        self._set_ready()
        self._complete_run_progress("Browser evidence complete")
        self.search_btn.setText("Run Evidence Search")
        self._log(f"Browser evidence lookup complete ({self._search_elapsed_text()})")

    def _browser_patient_finished(self, patient_evidence: dict) -> None:
        self._merge_browser_evidence(patient_evidence)
        self._refresh_evidence_table()
        self._update_evidence_summary()
        self._auto_rewrite_workbook()

    def _patient_report_outcome(self, outcome: PatientReportOutcome) -> None:
        self.report_outcomes[outcome.patient_id] = outcome
        self._refresh_operations_cockpit()
        if outcome.status == "written":
            self._log(f"Patient workbook ready: {outcome.path}")
        else:
            self._log(
                f"Patient workbook pending for {outcome.patient_id}; "
                "close it in Excel and resume to retry."
            )

    def _merge_browser_evidence(self, browser_evidence: dict) -> None:
        _merge_evidence_results(self.evidence, browser_evidence)

    def _browser_review_failed(self, message: str) -> None:
        worker = self.browser_worker
        if isinstance(worker, BrowserReviewWorker):
            failed_evidence = {}
            completed_sources = _completed_evidence_sources(self.evidence)
            protected_sources = _protected_remote_evidence_sources(self.evidence)
            for variant in worker.variants:
                key = BrowserReviewService.variant_key(variant)
                pending_databases = [
                    database
                    for database in worker.databases
                    if (key, database) not in completed_sources
                    and (key, database) not in protected_sources
                ]
                failed_evidence[key] = [
                    DatabaseEvidence(
                        database,
                        "error",
                        "Login-based lookup failed before a final result was captured. "
                        f"Details: {message}",
                    )
                    for database in pending_databases
                ]
            self._merge_browser_evidence(failed_evidence)
        self._refresh_evidence_table()
        self._auto_rewrite_workbook()
        self._worker_failed(message)

    def _stop_evidence_search(self) -> None:
        requested = False
        for thread, worker in (
            (self.database_thread, self.database_worker),
            (self.browser_thread, self.browser_worker),
        ):
            if (
                thread is not None
                and thread.isRunning()
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
        self.pause_search_btn.setText("Pause Search")
        self.stop_search_btn.setEnabled(False)
        self.stop_search_btn.setText("Stopping…")
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
                thread is not None
                and thread.isRunning()
                and isinstance(worker, (DatabaseWorker, BrowserReviewWorker))
            ):
                active_workers.append(worker)
        if not active_workers:
            return
        if self._search_pause_requested:
            for worker in active_workers:
                worker.resume_search()
            self._search_pause_requested = False
            self.pause_search_btn.setText("Pause Search")
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
        self.pause_search_btn.setText("Resume Search")
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
        self._refresh_evidence_table()
        self._auto_rewrite_workbook()
        self._set_ready()
        self.run_progress.show()
        self.run_progress.title.setText("Evidence search stopped")
        detail = (
            "Completed source results were kept and written to the review workbook. "
            "Resume Incomplete Search continues with only unfinished work."
        )
        if self.workbook_write_pending:
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
        self.search_btn.setText("Resume Incomplete Search")
        self._log(
            f"Evidence search stopped; completed results retained "
            f"({self._search_elapsed_text()})"
        )

    def _rewrite_workbook(self) -> None:
        if not self.result or not self.result.output_path:
            return
        if not self._try_write_evidence_workbook(show_errors=True):
            return
        if not self.run_progress.isHidden() and "complete" in self.run_progress.title.text().casefold():
            self.run_progress.detail.setText(
                "All queued patients have been processed and the workbook is updated."
            )
            self._set_search_complete_status(save_pending=False)
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
        if self._try_write_evidence_workbook(show_errors=False):
            if self.result and self.result.output_path:
                self._log(f"Evidence workbook updated: {self.result.output_path}")

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
        was_search = self._search_started_at is not None
        self._set_ready()
        if not self.run_progress.isHidden():
            self.run_progress.title.setText("Search stopped")
            self.run_progress.detail.setText(message)
            QTimer.singleShot(5000, self.run_progress.hide)
        if was_search:
            self.search_btn.setText("Resume Incomplete Search")
            self._log(f"ERROR after {self._search_elapsed_text()}: {message}")
        else:
            self._log(f"ERROR: {message}")
        QMessageBox.critical(self, "Error", message)

    def _save_settings(self, silent: bool = False) -> None:
        self.settings.default_output_dir = self.output_dir_edit.text()
        self.settings.clinvar_api_key = ""
        self.settings.cosmic_email = self.cosmic_email_edit.text()
        self.settings.cosmic_password = self.cosmic_password_edit.text()
        self.settings.oncokb_api_key = ""
        self.settings.oncokb_email = self.oncokb_email_edit.text()
        self.settings.oncokb_password = self.oncokb_password_edit.text()
        self.settings.franklin_api_key = ""
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
        self.settings.browser_background = self.browser_background_check.isChecked()
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
        self.variant_counters.setText(
            f"{self.result.total_count} total · "
            f"{len(self.result.included)} included · "
            f"{len(self.result.excluded)} excluded"
        )
        self._apply_review_filters()
        self._update_evidence_summary()
        self._refresh_operations_cockpit()

    def _refresh_operations_cockpit(self) -> None:
        if not hasattr(self, "status_matrix"):
            return
        variants = self.result.variants if self.result else []
        report_statuses = {
            patient_id: outcome.status
            for patient_id, outcome in self.report_outcomes.items()
        }
        self.status_matrix.set_rows(
            build_patient_status_rows(
                variants,
                databases=self.databases,
                evidence=self.evidence,
                skipped_keys=self.database_skip_keys,
                report_outcomes=report_statuses,
            )
        )
        self.retry_report_saves_button.setVisible(
            any(outcome.status == "pending" for outcome in self.report_outcomes.values())
        )

    def _retry_pending_report_saves(self) -> None:
        if self.result is None or self.result.output_path is None:
            return
        pending = {
            patient_id
            for patient_id, outcome in self.report_outcomes.items()
            if outcome.status == "pending"
        }
        if not pending:
            return
        coordinator = PatientReportCoordinator(
            self.result, self._variants_for_search(), self.evidence
        )
        coordinator.pending = pending
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
        self.report_retry_thread = thread
        thread.start()

    def _report_retry_finished(self, outcomes: list[PatientReportOutcome]) -> None:
        for outcome in outcomes:
            self._patient_report_outcome(outcome)

    def _activity_received(self, activity: RunActivity) -> None:
        self.current_activity.set_activity(activity)
        self.activity_timeline.add_activity(activity)
        self._log(activity.message or activity.action)

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
                "; ".join(
                    value
                    for value in [*variant.warnings, priority_warning(variant)]
                    if value
                ),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                highlight = variant_highlight(variant)
                if highlight == "artifact":
                    item.setBackground(QColor(Palette.artifact_orange))
                elif highlight == "artifact_light":
                    item.setBackground(QColor(Palette.artifact_light_orange))
                elif highlight == "germline":
                    item.setBackground(QColor(Palette.strong_green))
                elif highlight == "germline_low_af":
                    item.setBackground(QColor(Palette.pale_green))
                self.variant_table.setItem(row, col, item)
        self._apply_review_filters()

    def _apply_review_filters(self) -> None:
        if not hasattr(self, "variant_table"):
            return
        query = self.review_filter_edit.text().strip().casefold()
        mode = self.review_decision_combo.currentText()
        visible = 0
        for row in range(self.variant_table.rowCount()):
            row_text = " ".join(
                self._table_text(self.variant_table, row, column)
                for column in range(self.variant_table.columnCount())
            ).casefold()
            decision = self._table_text(self.variant_table, row, 5).casefold()
            mode_matches = (
                mode == "All decisions"
                or (mode == "Included" and decision == "included")
                or (mode == "Excluded" and decision == "excluded")
            )
            show = (not query or query in row_text) and mode_matches
            self.variant_table.setRowHidden(row, not show)
            visible += int(show)
        total = self.variant_table.rowCount()
        self.review_count_label.setText(
            f"Showing {visible} of {total}" if total else "No variants loaded"
        )
        self.variant_empty_state.setVisible(total > 0 and visible == 0)

    def _update_evidence_summary(self) -> None:
        if not hasattr(self, "evidence_summary"):
            return
        selected = [name for name, check in self.db_checks.items() if check.isChecked()]
        variant_count = len(self._variants_for_search()) if self.result else 0
        pending_count = (
            len(self._pending_variants_for_search(selected))
            if self.result and selected
            else variant_count
        )
        scope = "included variants" if self.included_only_check.isChecked() else "review variants"
        source_text = f"{len(selected)} source(s) selected" if selected else "No sources selected"
        variant_text = (
            f"{pending_count} pending of {variant_count} {scope}"
            if self.result
            else "process data to load variants"
        )
        browser_mode = (
            "Edge minimized"
            if hasattr(self, "browser_background_check")
            and self.browser_background_check.isChecked()
            else "Edge visible"
        )
        self.evidence_summary.setText(
            f"{source_text} · {variant_text} · {browser_mode}"
        )

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
        if self.workbook_write_pending:
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

    def _refresh_evidence_table(self) -> None:
        self.evidence_table.setRowCount(0)
        if not self.result:
            self.evidence_result_summary.setText(
                "No evidence collected yet. Process data before starting a search."
            )
            return
        self._refresh_operations_cockpit()
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
        row_count = self.evidence_table.rowCount()
        if row_count:
            source_count = sum(
                len(items) for items in evidence_by_key.values()
            )
            self.evidence_result_summary.setText(
                f"{row_count} variant(s) with {source_count} evidence result(s). Double-click a cell to inspect the full text."
            )
        else:
            self.evidence_result_summary.setText(
                "No evidence collected yet. Run selected sources to populate this table."
            )

    def _set_busy(self, label: str) -> None:
        self.run_progress.hide()
        is_search = label in {"Searching", "Browser lookups"}
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
        self.pause_search_btn.setText("Pause Search")
        self.pause_search_btn.setEnabled(is_search)
        self.stop_search_btn.setText("Stop Search")
        self.stop_search_btn.setEnabled(is_search)
        self.browser_signin_btn.setEnabled(False)
        self.browser_review_btn.setEnabled(False)
        self.rerun_failed_btn.setEnabled(False)
        self.rewrite_btn.setEnabled(False)
        self.patient_excel_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.status_bar.showMessage(label)

    def _set_ready(self) -> None:
        self.run_status_strip.set_snapshot(RunSnapshot())
        self.status_badge.setText("Ready")
        self.status_badge.setStyleSheet("")
        self.activity_progress.hide()
        self._update_process_state()
        self.search_btn.setEnabled(self.result is not None)
        self._search_pause_requested = False
        self._search_stop_requested = False
        self.pause_search_btn.setText("Pause Search")
        self.pause_search_btn.setEnabled(False)
        self.stop_search_btn.setText("Stop Search")
        self.stop_search_btn.setEnabled(False)
        self.browser_signin_btn.setEnabled(True)
        self.browser_review_btn.setEnabled(self.result is not None)
        self.rerun_failed_btn.setEnabled(
            self.result is not None
            and _has_failed_lookups(
                self._variants_for_search(), self.evidence, list(BROWSER_DATABASES)
            )
        )
        self.rewrite_btn.setEnabled(self.result is not None)
        self.patient_excel_btn.setEnabled(self.result is not None)
        self.resume_btn.setEnabled(True)
        self.status_bar.showMessage("Ready", 3000)

    def _update_process_state(self) -> None:
        has_input = Path(self.input_edit.text()).exists()
        has_output = bool(self.output_edit.text().strip())
        self.process_btn.setEnabled(has_input and has_output)

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{timestamp}] {message}")
        self.status_bar.showMessage(message, 5000)

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

    def _database_cell(self, evidence_items: list) -> str:
        return "\n".join(f"[{item.status}] {item.summary}".strip() for item in evidence_items)

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
                font-size: 20px;
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
                font-size: 26px;
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
                min-height: 20px;
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
            QFrame#MetricCard {{
                background: {Palette.panel};
                border: 1px solid {Palette.border};
                border-radius: 8px;
            }}
            QFrame#ToolbarCard, QFrame#VariantToolbar, QFrame#RunProgressCard,
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
            QLabel#VariantCounters {{
                color: {Palette.navy};
                background: {Palette.pale_blue};
                border-radius: 6px;
                padding: 6px 9px;
                font-weight: 700;
            }}
            QLabel#VariantEmptyState {{
                color: {Palette.muted};
                background: {Palette.panel};
                border: 1px dashed {Palette.border};
                border-radius: 8px;
                padding: 18px;
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
                background: #0B3A50;
                color: white;
                padding: 8px;
                border: none;
                border-right: 1px solid #1E5368;
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
