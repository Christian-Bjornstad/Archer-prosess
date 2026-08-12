# VPM Tolkning Operations Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the native VPM Tolkning interface as a compact operations cockpit with restart recovery, semantic run status, patient/provider tracking, and explicit report-save states.

**Architecture:** Add a presentation-only status model and focused PyQt widgets while keeping provider workers and clinical evidence services authoritative. `MainWindow` coordinates existing workers, status snapshots, and recent-workbook metadata; reusable shell, monitoring, and theme code moves out of the large `gui/app.py` file.

**Tech Stack:** Python 3.11+, PyQt6, openpyxl, existing local settings/audit services, pytest with offscreen Qt.

## Global Constraints

- Keep the current VPM Tolkning application icon unchanged.
- Remain a native PyQt6 Windows application; add no Node.js, browser UI framework, network service, or UI dependency.
- Preserve provider order: MTBP, Franklin, ClinVar, OncoKB, COSMIC.
- Never automatically contact provider websites on startup or workbook restoration.
- Store only local workbook paths and timestamps in recent-analysis metadata; never duplicate evidence or credentials into config.
- Preserve evidence parsing, clinical matching, stop/pause behavior, automatic patient reports, and workbook output formats.
- Status must use visible text plus colour or icon; never colour alone.
- Support the existing 1120 × 720 minimum and 1440 × 900 target without page-level horizontal scrolling.
- Keep normal-text contrast at or above 4.5:1 and primary controls at least 44 px high.
- Do not add dark mode, charts, PDF output, API controls, reference-profile copy, or review flags.

---

## File map

**Create:**

- `src/archer_processor/gui/status_model.py` — semantic run, source, and report state types plus pure aggregation functions.
- `src/archer_processor/gui/theme.py` — palette, spacing tokens, semantic state colours, and application QSS.
- `src/archer_processor/gui/icons.py` — local outline SVG icon loader for navigation and status marks.
- `src/archer_processor/gui/widgets/navigation.py` — icon-and-label sidebar.
- `src/archer_processor/gui/widgets/run_status.py` — persistent run strip and current-activity panel.
- `src/archer_processor/gui/widgets/status_matrix.py` — patient/provider/report status table.
- `src/archer_processor/gui/widgets/activity_timeline.py` — filtered timestamped activity view.
- `src/archer_processor/services/recent_analysis.py` — safe local recent-workbook metadata inspection.
- `tests/test_status_model.py` — pure state and aggregation tests.
- `tests/test_recent_analysis.py` — startup recovery metadata tests.

**Modify:**

- `src/archer_processor/gui/app.py` — assemble the new shell/workspaces and translate worker signals into status snapshots.
- `src/archer_processor/services/settings.py` — persist recent-workbook path and opt-in behavior.
- `src/archer_processor/services/browser_review.py` — expose structured provider activity without changing search behavior.
- `src/archer_processor/reports/patient_report_coordinator.py` — expose pending-save retry as a report-only action.
- `tests/test_gui.py` — shell, navigation, evidence cockpit, recovery, and action-state tests.
- `tests/test_settings.py` — recent metadata persistence and secret-exclusion tests.
- `tests/test_browser_review.py` — structured provider activity regression tests.
- `tests/test_patient_report_coordinator.py` — report-only retry tests.
- `README.md` and `docs/clinical_workflow.md` — operator-facing status and recovery documentation.
- `docs/assets/vpm-tolkning-evidence.png` — refreshed screenshot after visual approval.

---

### Task 1: Semantic operations status model

**Files:**

- Create: `src/archer_processor/gui/status_model.py`
- Create: `tests/test_status_model.py`

**Interfaces:**

- Consumes: `VariantRecord`, `DatabaseEvidence`, `PatientReportOutcome`, and `is_completed_evidence`.
- Produces: `RunPhase`, `CellState`, `RunActivity`, `RunSnapshot`, `StatusCell`, `PatientStatusRow`, `cell_state_for_evidence(...)`, and `build_patient_status_rows(...)`.

- [ ] **Step 1: Write failing tests for canonical labels and evidence states**

```python
from datetime import datetime

from archer_processor.core.models import DatabaseEvidence, VariantRecord
from archer_processor.gui.status_model import (
    CellState,
    RunActivity,
    RunPhase,
    cell_state_for_evidence,
)


def test_run_phases_have_stable_operator_labels():
    assert RunPhase.READY.label == "Ready"
    assert RunPhase.INTERRUPTED.label == "Interrupted · resume available"
    assert RunPhase.RETRY_AVAILABLE.label == "Complete · retry available"
    assert RunPhase.REPORT_PENDING.label == "Report save pending"


def test_retryable_evidence_maps_to_retry_not_complete():
    evidence = DatabaseEvidence("Franklin", "partial_capture", "recapture")
    assert cell_state_for_evidence(evidence) is CellState.RETRY


def test_activity_carries_structured_patient_provider_context():
    item = RunActivity(
        occurred_at=datetime(2026, 8, 12, 20, 0),
        patient_id="SYNTHETIC01",
        database="ClinVar",
        variant_label="TP53 c.524G>A",
        action="Capturing classification",
        message="Exact GRCh37 record verified",
    )
    assert item.database == "ClinVar"
    assert item.severity == "info"
```

- [ ] **Step 2: Run the new tests and verify the missing module failure**

Run: `python -m pytest tests/test_status_model.py -q`

Expected: FAIL with `ModuleNotFoundError: archer_processor.gui.status_model`.

- [ ] **Step 3: Implement the semantic enums and dataclasses**

```python
class RunPhase(str, Enum):
    READY = "ready"
    LOADING = "loading"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    INTERRUPTED = "interrupted"
    RETRY_AVAILABLE = "retry_available"
    COMPLETE = "complete"
    REPORT_PENDING = "report_pending"

    @property
    def label(self) -> str:
        return {
            RunPhase.READY: "Ready",
            RunPhase.LOADING: "Loading workbook",
            RunPhase.RUNNING: "Running",
            RunPhase.PAUSING: "Pausing",
            RunPhase.PAUSED: "Paused",
            RunPhase.STOPPING: "Stopping",
            RunPhase.INTERRUPTED: "Interrupted · resume available",
            RunPhase.RETRY_AVAILABLE: "Complete · retry available",
            RunPhase.COMPLETE: "Complete",
            RunPhase.REPORT_PENDING: "Report save pending",
        }[self]


class CellState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    NOT_FOUND = "not_found"
    RETRY = "retry"
    STOPPED = "stopped"
    SKIPPED = "skipped"
    REPORT_SAVED = "report_saved"
    SAVE_PENDING = "save_pending"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class RunActivity:
    occurred_at: datetime
    patient_id: str = ""
    database: str = ""
    variant_label: str = ""
    action: str = ""
    message: str = ""
    severity: str = "info"


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    phase: RunPhase = RunPhase.READY
    current_patient: int = 0
    patient_total: int = 0
    patient_id: str = ""
    database: str = ""
    variant_label: str = ""
    action: str = ""
    completed_sources: int = 0
    source_total: int = 0
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StatusCell:
    state: CellState
    label: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PatientStatusRow:
    patient_id: str
    variant_count: int
    cells: dict[str, StatusCell]


def cell_state_for_evidence(evidence: DatabaseEvidence) -> CellState:
    status = evidence.status.strip().casefold()
    if status == "not_found":
        return CellState.NOT_FOUND
    return CellState.COMPLETE if is_completed_evidence(evidence) else CellState.RETRY
```

Implement `cell_state_for_evidence` using the shared retry helper: `not_found`
maps to `NOT_FOUND`, completed terminal results map to `COMPLETE`, and retryable
results map to `RETRY`.

- [ ] **Step 4: Add aggregation tests for skipped variants and report outcomes**

```python
def synthetic_variants():
    return [
        VariantRecord(
            source_file=Path("synthetic.tsv"),
            source_row=2,
            sample="SYNTHETIC01_VPM_A",
            symbol="TP53",
            hgvsc="NM_000546.6:c.524G>A",
        )
    ]


def test_patient_rows_combine_source_and_report_state():
    rows = build_patient_status_rows(
        synthetic_variants(),
        databases=["ClinVar", "Franklin"],
        evidence={
            "SYNTHETIC01|NM_000546.6:c.524G>A": [
                DatabaseEvidence("ClinVar", "found", "Pathogenic")
            ]
        },
        skipped_keys=set(),
        report_outcomes={"SYNTHETIC01": "pending"},
        active=("SYNTHETIC01", "Franklin"),
    )
    assert rows[0].cells["ClinVar"].state is CellState.COMPLETE
    assert rows[0].cells["Franklin"].state is CellState.RUNNING
    assert rows[0].cells["Report"].state is CellState.SAVE_PENDING
```

- [ ] **Step 5: Implement deterministic patient-row aggregation**

Group variants by `patient_id` and use this implementation shape. The precedence
ensures one retryable variant keeps the whole patient/source cell actionable.

```python
STATE_LABELS = {
    CellState.QUEUED: "Queued",
    CellState.RUNNING: "Running",
    CellState.COMPLETE: "Complete",
    CellState.NOT_FOUND: "Not found",
    CellState.RETRY: "Retry",
    CellState.STOPPED: "Stopped",
    CellState.SKIPPED: "Skipped",
    CellState.REPORT_SAVED: "Report saved",
    CellState.SAVE_PENDING: "Save pending",
    CellState.NOT_READY: "Not ready",
}
STATE_PRIORITY = {
    CellState.RUNNING: 6,
    CellState.RETRY: 5,
    CellState.QUEUED: 4,
    CellState.NOT_FOUND: 3,
    CellState.COMPLETE: 2,
    CellState.SKIPPED: 1,
}


def build_patient_status_rows(
    variants: Sequence[VariantRecord],
    *,
    databases: Sequence[str],
    evidence: dict[str, list[DatabaseEvidence]],
    skipped_keys: set[str],
    report_outcomes: dict[str, str],
    active: tuple[str, str] | None = None,
) -> list[PatientStatusRow]:
    grouped: dict[str, list[VariantRecord]] = {}
    for variant in variants:
        grouped.setdefault(variant.patient_id, []).append(variant)
    rows = []
    for patient_id, patient_variants in grouped.items():
        cells: dict[str, StatusCell] = {}
        for database in databases:
            states = []
            for variant in patient_variants:
                key = f"{variant.sample}|{variant.hgvsc}"
                if active == (patient_id, database):
                    states.append(CellState.RUNNING)
                    continue
                if key in skipped_keys:
                    states.append(CellState.SKIPPED)
                    continue
                item = next(
                    (
                        candidate
                        for candidate in evidence.get(key, [])
                        if candidate.database == database
                    ),
                    None,
                )
                states.append(
                    CellState.QUEUED
                    if item is None
                    else cell_state_for_evidence(item)
                )
            state = max(states, key=STATE_PRIORITY.get)
            cells[database] = StatusCell(state, STATE_LABELS[state])
        report_status = report_outcomes.get(patient_id, "")
        report_state = {
            "written": CellState.REPORT_SAVED,
            "pending": CellState.SAVE_PENDING,
        }.get(report_status, CellState.NOT_READY)
        cells["Report"] = StatusCell(report_state, STATE_LABELS[report_state])
        rows.append(PatientStatusRow(patient_id, len(patient_variants), cells))
    return rows
```

- [ ] **Step 6: Run focused tests and commit**

Run: `python -m pytest tests/test_status_model.py tests/test_gui.py -q`

Expected: PASS.

```powershell
git add src/archer_processor/gui/status_model.py tests/test_status_model.py
git commit -m "feat: add semantic operations status model"
```

---

### Task 2: Theme, icons, navigation, and persistent shell

**Files:**

- Create: `src/archer_processor/gui/theme.py`
- Create: `src/archer_processor/gui/icons.py`
- Create: `src/archer_processor/gui/widgets/__init__.py`
- Create: `src/archer_processor/gui/widgets/navigation.py`
- Create: `src/archer_processor/gui/widgets/run_status.py`
- Modify: `src/archer_processor/gui/app.py:65-813,2460-2700`
- Modify: `tests/test_gui.py`

**Interfaces:**

- Consumes: `RunPhase`, `RunSnapshot`, existing app icon path, and page-switch callback.
- Produces: `Palette`, `application_stylesheet()`, `NavigationRail`, `RunStatusStrip.set_snapshot(snapshot)`, and `icon(name, color="#FFFFFF")`.

- [ ] **Step 1: Write failing shell tests**

```python
def test_navigation_uses_labels_without_numbered_workflow_copy(qt_app):
    window = MainWindow()
    labels = [button.text() for button in window.navigation.buttons]
    assert labels == ["Import", "Variants", "Evidence", "Settings"]
    assert all(not re.match(r"\d", label) for label in labels)


def test_run_status_strip_renders_interrupted_recovery_state(qt_app):
    strip = RunStatusStrip()
    strip.set_snapshot(
        RunSnapshot(
            phase=RunPhase.INTERRUPTED,
            current_patient=7,
            patient_total=28,
            patient_id="SYNTHETIC07",
        )
    )
    assert strip.phase_label.text() == "Interrupted · resume available"
    assert "7 / 28" in strip.progress_label.text()
    assert not strip.resume_button.isHidden()
```

- [ ] **Step 2: Run tests and verify the new widgets are missing**

Run: `python -m pytest tests/test_gui.py -k "navigation_uses or run_status_strip" -q`

Expected: FAIL because `NavigationRail` and `RunStatusStrip` do not exist.

- [ ] **Step 3: Extract palette and semantic QSS**

Move visual tokens from `app.py` into `theme.py`. Provide semantic widget
properties instead of per-event styles:

```python
STATE_COLORS = {
    RunPhase.READY: ("#E9F6EF", "#18794E", "#BDD9C3"),
    RunPhase.RUNNING: ("#E7F4F7", "#087EA4", "#99CDDA"),
    RunPhase.PAUSED: ("#FFF5D6", "#714600", "#D7AA4B"),
    RunPhase.INTERRUPTED: ("#FFF5D6", "#714600", "#D7AA4B"),
    RunPhase.RETRY_AVAILABLE: ("#FFF5D6", "#714600", "#D7AA4B"),
    RunPhase.REPORT_PENDING: ("#FFF5D6", "#714600", "#D7AA4B"),
    RunPhase.COMPLETE: ("#E9F6EF", "#18794E", "#BDD9C3"),
}
```

`application_stylesheet()` must include visible `:focus` borders, 44 px primary
buttons, 32 px compact table controls, and styles for `state` dynamic properties.

- [ ] **Step 4: Add a dependency-free local icon loader**

Store a small dictionary of outline SVG strings for `document`, `table`,
`search`, `settings`, `check`, `warning`, `error`, `pause`, and `stop`. Render with
`QSvgRenderer` into a transparent `QPixmap`; do not add a package dependency or
change the application icon.

- [ ] **Step 5: Build `NavigationRail` and `RunStatusStrip`**

```python
class NavigationRail(QFrame):
    page_requested = pyqtSignal(int)

    def __init__(self, app_icon_path: Path):
        super().__init__()
        self.setObjectName("NavigationRail")
        layout = QVBoxLayout(self)
        brand = QLabel()
        brand.setPixmap(QPixmap(str(app_icon_path)).scaled(48, 48))
        layout.addWidget(brand)
        layout.addWidget(QLabel("VPM Tolkning"))
        self.buttons = []
        for index, (label, icon_name) in enumerate(
            (("Import", "document"), ("Variants", "table"),
             ("Evidence", "search"), ("Settings", "settings"))
        ):
            button = QPushButton(label)
            button.setIcon(icon(icon_name))
            button.setCheckable(True)
            button.clicked.connect(
                lambda checked=False, page=index: self.page_requested.emit(page)
            )
            self.buttons.append(button)
            layout.addWidget(button)
        layout.addStretch()

    def set_current(self, index: int) -> None:
        self.buttons[index].setChecked(True)


class RunStatusStrip(QFrame):
    resume_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("RunStatusStrip")
        layout = QHBoxLayout(self)
        self.phase_label = QLabel("Ready")
        self.progress_label = QLabel("No active search")
        self.resume_button = QPushButton("Resume Incomplete Search")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        layout.addWidget(self.phase_label)
        layout.addWidget(self.progress_label)
        layout.addStretch()
        layout.addWidget(self.resume_button)
        layout.addWidget(self.pause_button)
        layout.addWidget(self.stop_button)

    def set_snapshot(self, snapshot: RunSnapshot) -> None:
        self.setProperty("phase", snapshot.phase.value)
        self.phase_label.setText(snapshot.phase.label)
        self.progress_label.setText(
            f"{snapshot.current_patient} / {snapshot.patient_total} patients"
        )
        self.resume_button.setVisible(
            snapshot.phase in {RunPhase.INTERRUPTED, RunPhase.RETRY_AVAILABLE}
        )
```

- [ ] **Step 6: Replace the current sidebar/header/progress-card assembly**

In `MainWindow._build_ui`, instantiate `self.navigation` and
`self.run_status_strip`. Keep page title/subtitle and the current app icon. Remove
numbered/descriptive navigation copy and the three always-visible metric cards
from the global shell; counts move into the Variants workspace in Task 4.

- [ ] **Step 7: Run shell tests and full GUI regression tests**

Run: `python -m pytest tests/test_gui.py -q`

Expected: PASS after updating assertions that deliberately referenced numbered
navigation or globally visible metric cards.

- [ ] **Step 8: Commit the shell slice**

```powershell
git add src/archer_processor/gui/theme.py src/archer_processor/gui/icons.py src/archer_processor/gui/widgets src/archer_processor/gui/app.py tests/test_gui.py
git commit -m "feat: introduce operations cockpit shell"
```

---

### Task 3: Safe recent-analysis recovery

**Files:**

- Create: `src/archer_processor/services/recent_analysis.py`
- Create: `tests/test_recent_analysis.py`
- Modify: `src/archer_processor/services/settings.py`
- Modify: `src/archer_processor/gui/app.py:815-905,1360-1410,1733-1810`
- Modify: `tests/test_settings.py`
- Modify: `tests/test_gui.py`

**Interfaces:**

- Consumes: processed-workbook path and sibling `*_browser_evidence` directory.
- Produces: `RecentAnalysis(path, modified_at, evidence_present, resume_available, valid, message)`, `inspect_recent_analysis(path)`, settings fields `last_processed_workbook` and `offer_recent_analysis`.

- [ ] **Step 1: Add failing settings and local inspection tests**

```python
def test_recent_workbook_path_persists_without_evidence(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: path))
    settings = AppSettings(last_processed_workbook="C:/local/review.xlsx")
    settings.save()
    payload = json.loads(path.read_text())
    assert payload["last_processed_workbook"] == "C:/local/review.xlsx"
    assert "evidence" not in payload


def test_recent_analysis_detects_local_resume_evidence(tmp_path):
    workbook = tmp_path / "review.xlsx"
    workbook.write_bytes(b"synthetic")
    evidence = tmp_path / "review_browser_evidence"
    evidence.mkdir()
    (evidence / "synthetic.audit.json").write_text(
        json.dumps({"retryable": True}), encoding="utf-8"
    )
    recent = inspect_recent_analysis(workbook)
    assert recent.valid
    assert recent.evidence_present
    assert recent.resume_available
```

- [ ] **Step 2: Run tests and verify missing fields/helper**

Run: `python -m pytest tests/test_recent_analysis.py tests/test_settings.py -q`

Expected: FAIL because recent-analysis metadata does not exist.

- [ ] **Step 3: Implement safe settings fields and inspection**

Add:

```python
last_processed_workbook: str = ""
offer_recent_analysis: bool = True
```

Implement `inspect_recent_analysis` with read-only `Path.exists`, suffix, stat,
and sibling-directory checks. Inspect audit JSON files in one bounded pass and
set `resume_available` when any schema-v2 audit has `retryable: true`; malformed
audits are ignored and reported in `message`. It must never open a browser or
instantiate a database service.

- [ ] **Step 4: Add failing Import recovery tests**

```python
def test_startup_offers_recent_analysis_without_loading_or_searching(
    qt_app, tmp_path, monkeypatch
):
    workbook = tmp_path / "review.xlsx"
    workbook.write_bytes(b"synthetic")
    monkeypatch.setattr(
        "archer_processor.gui.app.AppSettings.load",
        lambda: AppSettings(last_processed_workbook=str(workbook)),
    )
    calls = []
    monkeypatch.setattr(MainWindow, "_load_processed_workbook", lambda *a: calls.append(a))
    window = MainWindow()
    assert window.recent_analysis_panel.isVisibleTo(window)
    assert calls == []
```

- [ ] **Step 5: Redesign Import into New and Resume panels**

Show the recent workbook filename, modified timestamp, and `Resume data found`
when the evidence directory exists. Wire **Restore analysis** to the existing
asynchronous `_load_processed_workbook`. Wire **Dismiss** to hide the panel for
the current session. After processing or successful restore, update
`settings.last_processed_workbook` and save settings.

- [ ] **Step 6: Verify restoration makes no provider calls**

Run: `python -m pytest tests/test_recent_analysis.py tests/test_settings.py tests/test_gui.py -k "recent or processed_workbook or startup" -q`

Expected: PASS and no mocked browser/database call.

- [ ] **Step 7: Commit recovery slice**

```powershell
git add src/archer_processor/services/recent_analysis.py src/archer_processor/services/settings.py src/archer_processor/gui/app.py tests/test_recent_analysis.py tests/test_settings.py tests/test_gui.py
git commit -m "feat: offer safe recent-analysis recovery"
```

---

### Task 4: Dense variant review workspace

**Files:**

- Modify: `src/archer_processor/gui/app.py:906-936,2160-2255`
- Modify: `src/archer_processor/gui/theme.py`
- Modify: `tests/test_gui.py`

**Interfaces:**

- Consumes: existing variant result, filters, `variant_highlight`, and metrics refresh.
- Produces: inline counters, compact filter toolbar, explicit filtered-empty state, unchanged searchable table data.

- [ ] **Step 1: Write failing workspace structure tests**

```python
def loaded_window(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "sample_variants.tsv"
    window = MainWindow()
    window.result = VariantProcessor().process(
        fixture, "2026-08-12", tmp_path / "review.xlsx"
    )
    window._refresh_metrics()
    window._refresh_variant_table()
    return window


def test_variant_workspace_prioritises_toolbar_and_table(qt_app, tmp_path):
    window = loaded_window(tmp_path)
    assert window.variant_toolbar.objectName() == "VariantToolbar"
    assert window.variant_counters.text() == "5 total · 3 included · 2 excluded"
    assert window.variant_table.minimumHeight() >= 420
    assert not hasattr(window, "total_card")


def test_filtered_empty_state_explains_how_to_restore_rows(qt_app, tmp_path):
    window = loaded_window(tmp_path)
    window.review_filter_edit.setText("NO_SUCH_VARIANT")
    assert "Clear filters" in window.variant_empty_state.text()
```

- [ ] **Step 2: Run the tests and confirm the old metric-card layout fails**

Run: `python -m pytest tests/test_gui.py -k "variant_workspace or filtered_empty" -q`

Expected: FAIL because counters and empty state are absent.

- [ ] **Step 3: Rebuild `_review_tab` around one toolbar and table**

Use a `QFrame#VariantToolbar` containing search, decision filter, clear action,
and `variant_counters`. Keep the current columns and row-colour logic. Give the
table stretch priority and show an inline empty label only when no rows are
visible.

- [ ] **Step 4: Update `_refresh_metrics` and `_apply_review_filters`**

Set:

```python
self.variant_counters.setText(
    f"{result.total_count} total · {len(result.included)} included · "
    f"{len(result.excluded)} excluded"
)
self.variant_empty_state.setVisible(visible == 0)
```

Keep the existing review decision semantics and priority colours unchanged.

- [ ] **Step 5: Run variant and workbook regression tests**

Run: `python -m pytest tests/test_gui.py tests/test_processing.py tests/test_highlights.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/archer_processor/gui/app.py src/archer_processor/gui/theme.py tests/test_gui.py
git commit -m "feat: streamline variant review workspace"
```

---

### Task 5: Patient/provider status matrix and provider rows

**Files:**

- Create: `src/archer_processor/gui/widgets/status_matrix.py`
- Modify: `src/archer_processor/gui/app.py:937-1155,2180-2334`
- Modify: `src/archer_processor/gui/theme.py`
- Modify: `tests/test_gui.py`
- Modify: `tests/test_status_model.py`

**Interfaces:**

- Consumes: `PatientStatusRow`, selected providers, evidence, skip keys, and report outcomes.
- Produces: `StatusMatrix.set_rows(rows)`, `StatusMatrix.cell_activated(patient_id, database)`, compact provider rows, and cockpit refresh method `_refresh_operations_cockpit()`.

- [ ] **Step 1: Write failing matrix widget tests**

```python
def test_status_matrix_renders_text_for_every_state(qt_app):
    matrix = StatusMatrix(["ClinVar", "Franklin"])
    matrix.set_rows([
        PatientStatusRow(
            "SYNTHETIC01",
            2,
            {
                "ClinVar": StatusCell(CellState.COMPLETE, "Complete"),
                "Franklin": StatusCell(CellState.RETRY, "Retry"),
                "Report": StatusCell(CellState.SAVE_PENDING, "Save pending"),
            },
        )
    ])
    assert matrix.item(0, 2).text() == "Complete"
    assert matrix.item(0, 3).text() == "Retry"
    assert matrix.item(0, 4).text() == "Save pending"
```

- [ ] **Step 2: Run and verify the missing widget failure**

Run: `python -m pytest tests/test_gui.py -k status_matrix -q`

Expected: FAIL with missing `StatusMatrix`.

- [ ] **Step 3: Implement `StatusMatrix`**

Subclass `QTableWidget`; use columns `Patient`, `Variants`, five providers, and
`Report`. Store patient/provider in `Qt.ItemDataRole.UserRole`. Use local icons,
visible labels, tooltips, and semantic `state` data. Emit `cell_activated` on
double click or Enter.

- [ ] **Step 4: Replace provider tiles with compact provider rows**

Each row contains checkbox, provider name, purpose, local session label, and a
small `complete / retry / pending` count. Preserve the exact database checkbox
keys and settings persistence so search behavior does not change.

- [ ] **Step 5: Assemble the Evidence cockpit ordering**

Order the page as:

1. Queue command bar.
2. Current activity panel initialized with `No search is running`; Task 6 connects live activity.
3. Provider rows.
4. Patient/provider status matrix.
5. Collapsible timeline initialized with an explicit empty state; Task 6 connects live events.
6. Evidence result table and report recovery controls.

Remove the serial-worker spinbox from the visible UI because the value is fixed at
one. Preserve the underlying serial behavior.

- [ ] **Step 6: Derive and refresh matrix rows after every evidence/report update**

`_refresh_operations_cockpit()` calls `build_patient_status_rows` with the loaded
variants, selected providers, `self.evidence`, `self.database_skip_keys`, active
snapshot, and report outcomes. Call it from evidence merge, source selection,
workbook restoration, report outcome, stop, and completion handlers.

- [ ] **Step 7: Run focused and full GUI tests**

Run: `python -m pytest tests/test_status_model.py tests/test_gui.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add src/archer_processor/gui/widgets/status_matrix.py src/archer_processor/gui/app.py src/archer_processor/gui/theme.py tests/test_status_model.py tests/test_gui.py
git commit -m "feat: add patient evidence status matrix"
```

---

### Task 6: Structured activity, current task, and timeline

**Files:**

- Create: `src/archer_processor/gui/widgets/activity_timeline.py`
- Modify: `src/archer_processor/gui/widgets/run_status.py`
- Modify: `src/archer_processor/services/browser_review.py:200-390`
- Modify: `src/archer_processor/gui/app.py:203-555,1490-1630,1820-2005,2386-2400`
- Modify: `tests/test_browser_review.py`
- Modify: `tests/test_gui.py`

**Interfaces:**

- Consumes: worker patient loop, provider loop, browser-service progress messages.
- Produces: optional `activity: Callable[[str, str], None]` on `BrowserReviewService.search_variants`, worker signal `activity = pyqtSignal(object)`, `CurrentActivityPanel.set_snapshot`, and `ActivityTimeline.add_activity`.

- [ ] **Step 1: Add a failing structured browser-activity test**

```python
def test_browser_review_reports_provider_with_progress(tmp_path, monkeypatch):
    variant = ArcherTsvReader().read(FIXTURE)[0]
    service = BrowserReviewService(profile_root=tmp_path)
    seen = []
    monkeypatch.setattr(
        service,
        "_search_database",
        lambda database, variants, path, progress: {
            service.variant_key(variant): DatabaseEvidence(
                database, "not_found", "synthetic"
            )
        },
    )
    service.search_variants(
        [variant],
        ["Franklin"],
        tmp_path,
        activity=lambda database, message: seen.append((database, message)),
    )
    assert seen[0][0] == "Franklin"
```

- [ ] **Step 2: Run and verify the signature failure**

Run: `python -m pytest tests/test_browser_review.py -k reports_provider_with_progress -q`

Expected: FAIL because `activity` is not accepted.

- [ ] **Step 3: Add provider-aware activity without changing search order**

Add the optional callback to `search_variants`. Wrap provider progress:

```python
def provider_progress(message: str, *, database: str = database) -> None:
    if progress:
        progress(message)
    if activity:
        activity(database, message)
```

Use this callback for each `_search_*` call and emit `activity(database,
"Starting provider")` before navigation.

- [ ] **Step 4: Emit `RunActivity` from both search workers**

Add `activity = pyqtSignal(object)` to `DatabaseWorker` and
`BrowserReviewWorker`. At API and browser progress points emit patient ID,
database, variant label where known, action, and message. Preserve the existing
string `status` signal for log compatibility.

- [ ] **Step 5: Implement current activity and filtered timeline widgets**

`CurrentActivityPanel` shows patient, provider, variant, elapsed time, action,
patient progress, and overall progress. `ActivityTimeline` stores at most 2,000
events, exposes filters All/Warnings/Errors, and renders timestamps without
patient-identifying data beyond the pseudonymous DIT already visible in the app.

- [ ] **Step 6: Connect activity to the persistent strip and cockpit**

Add `_activity_received(activity)` to append the timeline, update the current
snapshot, refresh the status matrix active cell, and keep `_log(activity.message)`
for the existing plain-text audit log.

- [ ] **Step 7: Add GUI tests for active task and timeline filtering**

```python
def test_activity_updates_current_patient_provider_and_timeline(qt_app):
    window = MainWindow()
    window._activity_received(
        RunActivity(
            datetime.now(), "SYNTHETIC02", "Franklin",
            "TP53 c.524G>A", "Capturing", "Population frequencies",
        )
    )
    assert window.current_activity.provider_value.text() == "Franklin"
    assert window.activity_timeline.rowCount() == 1
```

- [ ] **Step 8: Run browser and GUI regression tests**

Run: `python -m pytest tests/test_browser_review.py tests/test_gui.py -q`

Expected: PASS with provider order and existing timestamps unchanged.

- [ ] **Step 9: Commit**

```powershell
git add src/archer_processor/services/browser_review.py src/archer_processor/gui/widgets/run_status.py src/archer_processor/gui/widgets/activity_timeline.py src/archer_processor/gui/app.py tests/test_browser_review.py tests/test_gui.py
git commit -m "feat: track structured evidence activity"
```

---

### Task 7: Report-save recovery without database reruns

**Files:**

- Modify: `src/archer_processor/reports/patient_report_coordinator.py`
- Modify: `src/archer_processor/gui/app.py:1827-1845,1917-1955,2256-2334`
- Modify: `tests/test_patient_report_coordinator.py`
- Modify: `tests/test_gui.py`

**Interfaces:**

- Consumes: `PatientReportOutcome`, coordinator pending set, current evidence.
- Produces: `PatientReportCoordinator.retry_pending()`, `ReportRetryWorker`, `MainWindow._retry_pending_report_saves()`, and report outcome map used by the status matrix.

- [ ] **Step 1: Write a failing report-only retry test**

```python
def two_patient_result(tmp_path):
    variants = [
        VariantRecord(
            source_file=tmp_path / "synthetic.tsv",
            source_row=index,
            sample=f"SYNTHETIC0{index}_VPM_A",
            symbol=symbol,
            hgvsc=hgvsc,
        )
        for index, symbol, hgvsc in (
            (1, "TP53", "NM_000546.6:c.524G>A"),
            (2, "KRAS", "NM_004985.5:c.35G>A"),
        )
    ]
    result = ProcessingResult(
        input_path=tmp_path / "synthetic.tsv",
        output_path=tmp_path / "review.xlsx",
        run_date="2026-08-12",
        variants=variants,
        rules_applied=[],
    )
    return result, variants


def test_retry_pending_rewrites_only_locked_reports(tmp_path, monkeypatch):
    result, variants = two_patient_result(tmp_path)
    coordinator = PatientReportCoordinator(result, variants, {})
    coordinator.pending = {"SYNTHETIC02"}
    calls = []
    monkeypatch.setattr(
        coordinator,
        "write_patient",
        lambda patient_id: calls.append(patient_id)
        or PatientReportOutcome(patient_id, tmp_path / "out.xlsx", "written", "ok"),
    )
    coordinator.retry_pending()
    assert calls == ["SYNTHETIC02"]
```

- [ ] **Step 2: Run and verify the missing method failure**

Run: `python -m pytest tests/test_patient_report_coordinator.py -k retry_pending -q`

Expected: FAIL with missing `retry_pending`.

- [ ] **Step 3: Implement report-only pending retry**

```python
def retry_pending(self) -> list[PatientReportOutcome]:
    return [self.write_patient(patient_id) for patient_id in sorted(self.pending)]
```

Do not call any database or browser service from this method.

- [ ] **Step 4: Persist report outcomes in the window state**

Maintain `self.report_outcomes: dict[str, PatientReportOutcome]`. Update it from
worker signals, refresh the matrix, and set `RunPhase.REPORT_PENDING` when any
outcome is pending.

- [ ] **Step 5: Add and wire `Retry Pending Saves`**

Show the action only when pending outcomes exist. Reconstruct a coordinator from
the loaded result, selected variants, and current evidence; seed its pending set;
call `retry_pending()` in this focused worker so large workbooks do not freeze the
UI. Update only report cells and completion state.

```python
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
```

- [ ] **Step 6: Add a GUI test proving no evidence worker starts**

```python
def test_report_retry_worker_does_not_start_database_search(
    qt_app, tmp_path, monkeypatch
):
    result, variants = two_patient_result(tmp_path)
    coordinator = PatientReportCoordinator(result, variants, {})
    coordinator.pending = {"SYNTHETIC02"}
    monkeypatch.setattr(
        DatabaseSearchService,
        "search_variant",
        lambda *args: pytest.fail("database search must not run"),
    )
    monkeypatch.setattr(
        BrowserReviewService,
        "search_variants",
        lambda *args: pytest.fail("browser search must not run"),
    )
    outcomes = []
    worker = ReportRetryWorker(coordinator)
    worker.finished.connect(outcomes.extend)
    worker.run()
    assert outcomes[0].patient_id == "SYNTHETIC02"
    assert outcomes[0].status == "written"


def test_pending_report_exposes_retry_action(qt_app, tmp_path):
    result, _ = two_patient_result(tmp_path)
    window = MainWindow()
    window.result = result
    window.report_outcomes = {
        "SYNTHETIC02": PatientReportOutcome(
            "SYNTHETIC02",
            tmp_path / "SYNTHETIC02_VPM_Tolkning.xlsx",
            "pending",
            "locked",
        )
    }
    window._refresh_operations_cockpit()
    assert not window.retry_report_saves_button.isHidden()
```

- [ ] **Step 7: Run report, GUI, and Excel tests**

Run: `python -m pytest tests/test_patient_report_coordinator.py tests/test_patient_excel.py tests/test_gui.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add src/archer_processor/reports/patient_report_coordinator.py src/archer_processor/gui/app.py tests/test_patient_report_coordinator.py tests/test_gui.py
git commit -m "feat: retry pending patient report saves"
```

---

### Task 8: Settings consolidation, visual QA, and operator documentation

**Files:**

- Modify: `src/archer_processor/gui/app.py:1156-1360`
- Modify: `src/archer_processor/gui/theme.py`
- Modify: `tests/test_gui.py`
- Modify: `README.md`
- Modify: `docs/clinical_workflow.md`
- Modify: `docs/assets/vpm-tolkning-evidence.png`

**Interfaces:**

- Consumes: completed shell, status model, recovery metadata, matrix, activity, and report states.
- Produces: four settings sections, final visual snapshots, and documented operator behavior.

- [ ] **Step 1: Write a failing settings-structure test**

```python
def test_settings_are_grouped_into_four_operator_sections(qt_app):
    window = MainWindow()
    titles = [group.title() for group in window.settings_groups]
    assert titles == [
        "Local files",
        "Browser access",
        "Search pacing",
        "Artifact rules",
    ]
```

- [ ] **Step 2: Run and verify the current settings structure fails**

Run: `python -m pytest tests/test_gui.py -k settings_are_grouped -q`

Expected: FAIL because the canonical group list is absent.

- [ ] **Step 3: Consolidate settings without changing stored meaning**

Rebuild `_settings_tab_v2` with the four approved groups. Preserve all existing
field objects and `_save_settings` bindings. Keep credentials masked. Add the
recent-analysis opt-in under Local files. Remove duplicated helper copy, but do
not remove any existing provider credential field.

- [ ] **Step 4: Add a deterministic offscreen screenshot harness**

Add test helpers, not a production dependency, that instantiate `MainWindow`, set
synthetic Ready/Running/Paused/Interrupted/Retry/Report-pending snapshots, resize
to 1120 × 720 and 1440 × 900, process events, and save `window.grab()` output to a
temporary directory. Assert each image has the requested dimensions and nonzero
content variance.

- [ ] **Step 5: Run the UI Pro Max pre-delivery checklist**

Read `C:\Users\molpa\.codex\skills\ui-ux-pro-max\references\pro-rules.md` and
verify:

- icon consistency and no emoji icons;
- visible focus on all interactive controls;
- text contrast and colour-independent status labels;
- 44 px primary actions and usable table controls;
- no page-level horizontal scrolling at both target sizes;
- no content hidden behind navigation or status strip;
- clear Ready, loading, empty, interrupted, error, retry, and complete states.

- [ ] **Step 6: Inspect all rendered states and iterate on QSS/layout**

Use local image inspection on every rendered PNG. Correct clipping, excess empty
space, weak hierarchy, misaligned baselines, and low contrast. Do not change the
application icon. Replace `docs/assets/vpm-tolkning-evidence.png` only after the
Evidence Running state is visually approved.

- [ ] **Step 7: Update operator documentation**

Document:

- safe Restore analysis behavior after interruption;
- meaning of every run and matrix state;
- provider search is never automatic after restart;
- patient report Saved/Save pending states and Retry Pending Saves;
- timeline filters and current activity;
- unchanged provider order and output behavior.

- [ ] **Step 8: Run formatting, secret, compile, and full test checks**

Run:

```powershell
git diff --check
python -m compileall -q src
python -m pytest -q
rg -n -i "password\s*=\s*['\"][^'\"]+|bearer\s+[A-Za-z0-9._-]+" src tests docs README.md
```

Expected: no whitespace errors, successful compilation, all tests passing, and no
real credential values. Synthetic test strings such as `secret` may appear only
inside credential-storage unit tests.

- [ ] **Step 9: Review the complete branch**

Run:

```powershell
git status --short
git log --oneline main..HEAD
git diff main...HEAD --stat
```

Review correctness, readability, architecture, security, and performance. Confirm
that no evidence parsing, clinical matching, or workbook format changed.

- [ ] **Step 10: Commit documentation and visual polish**

```powershell
git add src/archer_processor/gui/app.py src/archer_processor/gui/theme.py tests/test_gui.py README.md docs/clinical_workflow.md docs/assets/vpm-tolkning-evidence.png
git commit -m "docs: describe operations cockpit workflow"
```

---

## Final acceptance checklist

- [ ] A recent interrupted workbook can be restored locally without opening Edge.
- [ ] The persistent strip accurately distinguishes Ready, Running, Paused,
  Interrupted, Retry available, Complete, and Report save pending.
- [ ] The current patient, provider, variant, action, elapsed time, and progress
  remain visible during long searches.
- [ ] Every selected patient/provider/report combination has a visible text state.
- [ ] Stop and resume retain completed evidence and do not re-run completed sources.
- [ ] Retry Pending Saves writes reports without starting database searches.
- [ ] The Variants table gains usable space and preserves existing decisions and
  clinical priority colours.
- [ ] Settings retain all current functionality in four concise groups.
- [ ] The current VPM icon is unchanged.
- [ ] All automated tests and visual target-size checks pass.
