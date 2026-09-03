# Clinical Rules and Excel Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the v1 artifact entries, implement the ASXL1 display band, and make review and patient workbooks follow the approved AF/manual-field contract.

**Architecture:** Keep exclusion logic in `core/rules.py`, presentation categories in `core/highlights.py`, and workbook-specific behavior in report writers. Introduce a small manual-field helper so regeneration preserves operator-entered data by variant identity rather than row number.

**Tech Stack:** Python 3.11+, pytest, openpyxl, PyQt6 application models

**Spec:** `docs/superpowers/specs/2026-09-03-vpm-app-improvements-design.md`

## Global Constraints

- Baseline is `origin/main`.
- ASXL1 is excluded at AF `<= 5.5%`; the new light-orange band is presentation-only for AF `> 5.0%` and `<= 5.5%`.
- AF cells remain numeric and use Excel percentage formatting.
- Manual `Kommentar` and `HSMD` content must survive regeneration by stable variant identity.
- Do not rename `With Artifacts` or `Artifacts Removed`.

---

### Task 1: Artifact catalog v3 and ASXL1 boundaries

**Files:**
- Modify: `src/archer_processor/core/rules.py`
- Modify: `src/archer_processor/services/settings.py`
- Test: `tests/test_processing.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: `default_artifact_rules() -> list[dict[str, str]]`
- Produces: catalog version 3 and unchanged `FilterEngine.apply()` exclusion semantics

- [ ] **Step 1: Write failing catalog and migration tests**

```python
def test_default_catalog_adds_fragmentation_v1_cebpa_entries():
    hgvsc = {entry["hgvsc"] for entry in default_artifact_rules()}
    assert {
        "NM_004364.4:c.288C>G",
        "NM_004364.4:c.280G>C",
        "NM_004364.4:c.296G>C",
    } <= hgvsc

def test_settings_migrates_exact_v2_catalog_but_preserves_custom_catalog(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    new_hgvsc = {
        "NM_004364.4:c.288C>G",
        "NM_004364.4:c.280G>C",
        "NM_004364.4:c.296G>C",
    }
    former_v2 = [
        entry for entry in default_artifact_rules()
        if entry["hgvsc"] not in new_hgvsc
    ]
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config))
    config.write_text(json.dumps({
        "artifact_catalog_version": 2,
        "artifact_rules": former_v2,
    }), encoding="utf-8")
    assert AppSettings.load().artifact_rules == default_artifact_rules()

    custom = [{"gene": "CUSTOM", "hgvsc": "NM_1:c.1A>G", "max_af": ""}]
    config.write_text(json.dumps({
        "artifact_catalog_version": 2,
        "artifact_rules": custom,
    }), encoding="utf-8")
    assert AppSettings.load().artifact_rules == custom
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `pytest tests/test_processing.py tests/test_settings.py -q`

Expected: failures for the three missing HGVSc values and catalog version `2`.

- [ ] **Step 3: Add the three rules and migrate exact former defaults**

Add the three CEBPA dictionaries to `default_artifact_rules()`. In settings, set `artifact_catalog_version: int = 3`, keep a private exact set of former v2 HGVSc values, and use this condition:

```python
if int(data.get("artifact_catalog_version", 0) or 0) < 3:
    if configured_hgvsc == _V2_DEFAULT_HGVSC:
        settings.artifact_rules = default_artifact_rules()
    settings.artifact_catalog_version = 3
```

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/test_processing.py tests/test_settings.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the catalog change**

```bash
git add src/archer_processor/core/rules.py src/archer_processor/services/settings.py tests/test_processing.py tests/test_settings.py
git commit -m "feat: add fragmentation v1 artifacts"
```

### Task 2: Light-orange ASXL1 presentation category

**Files:**
- Modify: `src/archer_processor/core/highlights.py`
- Modify: `src/archer_processor/reports/excel_report.py`
- Modify: `src/archer_processor/reports/patient_excel.py`
- Modify: `src/archer_processor/gui/app.py`
- Test: `tests/test_highlights.py`
- Test: `tests/test_processing.py`
- Test: `tests/test_patient_excel.py`
- Test: `tests/test_gui.py`

**Interfaces:**
- Consumes: `VariantRecord.af`, `VariantRecord.hgvsc`, and artifact decision
- Produces: `variant_highlight(variant) == "artifact_light"` only for the approved ASXL1 band

- [ ] **Step 1: Write exact boundary tests**

```python
@pytest.mark.parametrize(
    ("af", "highlight", "decision"),
    [(0.05, "artifact", "excluded"), (0.050001, "artifact_light", "excluded"),
     (0.055, "artifact_light", "excluded"), (0.055001, "", "included")],
)
def test_asxl1_artifact_color_band(af, highlight, decision):
    variant = make_variant(hgvsc="NM_015338.5:c.1934dup", af=af)
    FilterEngine(production_rules()).apply([variant])
    assert variant.decision == decision
    assert variant_highlight(variant) == highlight
```

Add workbook and GUI assertions for distinct strong/light orange RGB values.

- [ ] **Step 2: Run boundary tests and confirm failure**

Run: `pytest tests/test_highlights.py tests/test_processing.py tests/test_patient_excel.py tests/test_gui.py -q`

Expected: the `artifact_light` cases fail.

- [ ] **Step 3: Implement presentation category and colors**

Return `artifact_light` before the generic artifact branch when HGVSc matches and `0.05 < af <= 0.055`. Add a pale orange color token and map it consistently in both Excel writers and the GUI table.

- [ ] **Step 4: Run boundary tests**

Run: `pytest tests/test_highlights.py tests/test_processing.py tests/test_patient_excel.py tests/test_gui.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the display band**

```bash
git add src/archer_processor/core/highlights.py src/archer_processor/reports/excel_report.py src/archer_processor/reports/patient_excel.py src/archer_processor/gui/app.py tests
git commit -m "feat: show ASXL1 light orange AF band"
```

### Task 3: Numeric AF formatting and stable sorting

**Files:**
- Create: `src/archer_processor/core/sorting.py`
- Modify: `src/archer_processor/reports/excel_report.py`
- Modify: `src/archer_processor/reports/patient_excel.py`
- Test: `tests/test_processing.py`
- Test: `tests/test_patient_excel.py`

**Interfaces:**
- Produces: `variant_sort_key(variant) -> tuple[str, int, float, str, str]`

- [ ] **Step 1: Write failing workbook-order tests**

Create two patients with AF values `0.10`, `None`, and `0.25`. Assert patient grouping is stable, AF values are numeric, `number_format == "0.00%"`, and rows inside each patient are ordered `0.25`, `0.10`, `None` in both review sheets and `Oversikt`.

- [ ] **Step 2: Run the new tests and confirm failure**

Run: `pytest tests/test_processing.py tests/test_patient_excel.py -q`

Expected: current raw ordering or `0.0000` formatting fails.

- [ ] **Step 3: Add one shared deterministic key**

Create `src/archer_processor/core/sorting.py` with:

```python
def variant_sort_key(variant: VariantRecord) -> tuple[str, int, float, str, str]:
    missing = 1 if variant.af is None else 0
    descending_af = 0.0 if variant.af is None else -variant.af
    return (variant.patient_id, missing, descending_af, variant.symbol, variant.hgvsc)
```

Use it before writing both review sheets and each patient overview. Write raw `variant.af` to AF cells and set `0.00%`.

- [ ] **Step 4: Run report tests**

Run: `pytest tests/test_processing.py tests/test_patient_excel.py -q`

Expected: PASS.

- [ ] **Step 5: Commit AF contract**

```bash
git add src/archer_processor/core/sorting.py src/archer_processor/reports/excel_report.py src/archer_processor/reports/patient_excel.py tests/test_processing.py tests/test_patient_excel.py
git commit -m "feat: format and sort variant AF values"
```

### Task 4: Preserve manual Kommentar and HSMD

**Files:**
- Create: `src/archer_processor/reports/manual_fields.py`
- Modify: `src/archer_processor/reports/patient_excel.py`
- Test: `tests/test_patient_excel.py`

**Interfaces:**
- Produces: `read_manual_fields(path: Path, patient_id: str) -> dict[str, ManualVariantFields]`
- Produces: `variant_manual_key(patient_id: str, variant: VariantRecord) -> str`
- Consumes: existing patient workbook before overwrite

- [ ] **Step 1: Write a regeneration test**

Generate a workbook, enter `Vurdert manuelt` in `Kommentar`, replace `HSMD -` with `HSMD - intern klassifikasjon`, reorder the variants by changing AF, regenerate, and assert both texts follow the same HGVSc to its new row.

- [ ] **Step 2: Run the regeneration test and confirm failure**

Run: `pytest tests/test_patient_excel.py -q`

Expected: the manual values disappear.

- [ ] **Step 3: Implement stable extraction and merge**

Use this immutable type:

```python
@dataclass(frozen=True, slots=True)
class ManualVariantFields:
    comment: str = ""
    hsmd: str = "HSMD -"
```

Read `Oversikt` headers by label, identify rows from `Gen` + `HGVSc`, and extract only the `Kommentar` cell and the line beginning `HSMD`. Add `Kommentar` after `Kort evidens`; rebuild automatic evidence lines while injecting the preserved HSMD line.

- [ ] **Step 4: Run patient workbook tests**

Run: `pytest tests/test_patient_excel.py -q`

Expected: PASS.

- [ ] **Step 5: Commit manual-field preservation**

```bash
git add src/archer_processor/reports/manual_fields.py src/archer_processor/reports/patient_excel.py tests/test_patient_excel.py
git commit -m "feat: preserve manual patient report fields"
```

### Task 5: Delivery verification and documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/clinical_workflow.md`

- [ ] **Step 1: Update documented artifact boundaries, catalog entries, AF format, and manual fields**

State the exact `<=5.0%`, `>5.0%..<=5.5%`, and `>5.5%` behavior and list the three CEBPA additions.

- [ ] **Step 2: Run the complete suite**

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 3: Commit documentation**

```bash
git add README.md docs/clinical_workflow.md
git commit -m "docs: describe clinical Excel contract"
```
