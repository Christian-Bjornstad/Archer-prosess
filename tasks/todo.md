# Prioriterte pasienter først

## Task 1: Definer pasientomfang

**Description:** Lag eksplisitte funksjoner for markerte og resterende
pasient-ID-er uten å bruke dagens «ingen markering betyr alle»-semantikk.

**Acceptance criteria:**
- [x] Markerte rader gir unike pasient-ID-er i synlig rekkefølge.
- [x] Tomt valg gir tom valgt-kø.
- [x] Restomfang inkluderer bare pasienter med uferdig valgt evidens.

**Verification:**
- [x] Fokuserte tester i `tests/test_gui.py`.

**Dependencies:** None

**Files likely touched:**
- `src/archer_processor/gui/app.py`
- `tests/test_gui.py`

**Estimated scope:** Small

## Task 2: Koble omfang til søkekøen

**Description:** Filtrer den eksisterende resume-bevisste variantkøen med det
valgte pasientomfanget før worker-en opprettes.

**Acceptance criteria:**
- [x] Bare valgte pasienters varianter sendes til søket.
- [x] Germline, X-markerte og fullførte oppslag filtreres som før.
- [x] Originale pasientindekser og evidensmapper bevares.

**Verification:**
- [x] Worker-tester viser riktig valgt og resterende kø.

**Dependencies:** Task 1

**Files likely touched:**
- `src/archer_processor/gui/app.py`
- `tests/test_gui.py`

**Estimated scope:** Small

## Checkpoint: Utvalg og kø

- [x] GUI-testene består.
- [x] Ingen valgt-kjøring kan starte alle ved tomt valg.

## Task 3: Legg til brukerhandlinger og status

**Description:** Vis antall valgte pasienter og legg til handlingene «Kjør
valgte pasienter» og «Kjør resterende» med riktig aktiv/deaktivert tilstand.

**Acceptance criteria:**
- [x] Knappestatus følger radvalg og aktiv worker.
- [x] Status viser kjøretype, aktiv pasient og antall som gjenstår.
- [x] Tom eller ferdig kø gir en tydelig melding.

**Verification:**
- [x] Qt-tester verifiserer tekst, knappestatus og hendelser.
- [x] Manuell syntetisk GUI-kontroll.

**Dependencies:** Tasks 1-2

**Files likely touched:**
- `src/archer_processor/gui/app.py`
- `src/archer_processor/gui/widgets/status_matrix.py`
- `tests/test_gui.py`

**Estimated scope:** Medium

## Task 4: Generer prioriterte rapporter

**Description:** Etter ferdig søk og vellykket lagring, send nøyaktig samme
pasientgruppe til eksisterende `PatientReportWorker`.

**Acceptance criteria:**
- [x] Rapportfasen starter ikke før evidens er flettet og lagret.
- [x] Bare prioriterte pasienter rapporteres i denne fasen.
- [x] Låste eller feilede filer stopper ikke resten av gruppen.

**Verification:**
- [x] GUI-tester av søk → lagring → rapport-rekkefølgen.
- [x] `tests/test_patient_report_coordinator.py` består.

**Dependencies:** Tasks 2-3

**Files likely touched:**
- `src/archer_processor/gui/app.py`
- `tests/test_gui.py`
- `tests/test_patient_report_coordinator.py`

**Estimated scope:** Medium

## Task 5: Kjør resten og gjenoppta etter omstart

**Description:** Beregn restkøen fra lagret evidens, både i samme sesjon og
etter at en behandlet arbeidsbok er lastet på nytt.

**Acceptance criteria:**
- [x] Ferdige prioriterte pasienter utelates når de ikke har uferdig arbeid.
- [x] Delvis ferdige pasienter inkluderer bare uferdige kilder.
- [x] Tom restkø starter ingen worker.

**Verification:**
- [x] Tester for samme sesjon og gjenlastet arbeidsbok.
- [ ] Manuell totrinnskjøring med syntetiske data.

**Dependencies:** Tasks 2-4

**Files likely touched:**
- `src/archer_processor/gui/app.py`
- `src/archer_processor/services/processed_workbook.py`
- `tests/test_gui.py`
- `tests/test_processed_workbook.py`

**Estimated scope:** Medium

## Checkpoint: Komplett arbeidsflyt

- [x] Pause, stopp og resume fungerer i begge kjøretypene.
- [x] Prioriterte rapporter finnes før restkøen startes.
- [x] Ingen fullførte oppslag gjentas.

## Task 6: Dokumentasjon og full verifikasjon

**Description:** Dokumenter arbeidsflyten og gjennomfør full regresjonskontroll.

**Acceptance criteria:**
- [x] Brukerdokumentasjonen forklarer markering, prioritert kjøring og restkø.
- [x] Alle fokuserte og komplette tester består.

**Verification:**
- [x] `python -m pytest -q`
- [x] Visuell kontroll av GUI med syntetiske pasient-ID-er.

**Dependencies:** Tasks 1-5

**Files likely touched:**
- `README.md`
- `docs/clinical_workflow.md`

**Estimated scope:** Small
