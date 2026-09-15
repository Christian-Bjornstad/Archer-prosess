# Implementation Plan: Prioriterte pasienter først

## Overview

Legg til en avgrenset arbeidsflyt der brukeren kan markere én eller flere
pasienter i statusoversikten, kjøre alle uferdige databasesøk for disse, lage
rapportene deres og deretter fortsette med resten av pasientene uten å gjenta
ferdig arbeid.

## Architecture Decisions

- Bruk eksisterende radvalg i `StatusMatrix`; pasientnummer skal ikke skrives
  manuelt i et nytt tekstfelt.
- Skill mellom «Kjør valgte pasienter» og «Kjør resterende», slik at tomt
  utvalg aldri utilsiktet betyr alle pasienter.
- Filtrer den eksisterende variantkøen før `DatabaseWorker` eller
  `BrowserReviewWorker` opprettes. Søke-, pause-, stopp- og resume-logikken
  beholdes.
- Eksisterende evidens og fullførte variant/database-par skal være fasit for
  restkøen. Prioritering er kjørerekkefølge, ikke en ny lagret klinisk status.
- Start rapportgenerering for den prioriterte gruppen først etter at evidensen
  er flettet og arbeidsboken er lagret. Rapportskriving forblir adskilt fra
  nettlesersøkene.

## Dependency Flow

Pasientutvalg → køfiltrering → databasesøk → evidenslagring → rapporter for
valgte → beregning og kjøring av restkø.

## Task List

### Phase 1: Utvalg og kø

1. Definer eksplisitt valgt og resterende pasientomfang.
2. Koble pasientomfanget til den resume-bevisste variantkøen.

### Checkpoint: Kø

- Valgte pasienter er de eneste som sendes til worker-en.
- Tomt valg starter ikke alle via «Kjør valgte pasienter».
- Ferdige og automatisk utelatte oppslag forblir utelatt.

### Phase 2: Brukerflyt og rapporter

3. Legg til handlinger og tydelig status for valgt og resterende kjøring.
4. Generer rapporter for den ferdige prioriterte gruppen.
5. Beregn og start restkøen fra lagret evidens.

### Checkpoint: Ende-til-ende

- Prioriterte pasienter får ferdige rapporter før resten kjøres.
- Restkøen gjentar ikke fullførte oppslag.
- Pause, stopp, resume og låste rapportfiler følger eksisterende oppførsel.

### Phase 3: Robusthet

6. Verifiser gjenopptak etter omstart og tom restkø.
7. Oppdater brukerrettet dokumentasjon og kjør full regresjonstest.

## Acceptance Criteria

- Brukeren kan markere én eller flere pasientrader og kjøre bare disse.
- Appen viser antall valgte pasienter og nekter valgt-kjøring uten utvalg.
- Når prioritert søk er ferdig, kan rapportene for nøyaktig samme gruppe lages.
- «Kjør resterende» inkluderer bare pasienter med minst ett uferdig valgt
  databaseoppslag.
- Fullført evidens overlever stopp, omstart og lasting av behandlet arbeidsbok.
- En låst rapportfil stopper ikke rapporter for andre pasienter.

## Verification

- Fokuserte GUI- og worker-tester i `tests/test_gui.py`.
- Rapportkoordinator-tester i `tests/test_patient_report_coordinator.py`.
- Manuell kontroll med syntetiske pasient-ID-er: kjør to valgte, bekreft
  rapporter, start resten og bekreft at de to første ikke søkes på nytt.
- Full testkjøring med `python -m pytest -q`.

## Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Tomt radvalg tolkes som alle | Høy | Separate scope-funksjoner og deaktivert valgt-knapp uten markering |
| Prioriterte pasienter søkes på nytt | Høy | Bygg kø fra eksisterende `completed_sources` |
| Rapport lages før evidens er lagret | Høy | Start rapportfasen først etter merge og vellykket arbeidsbokskriving |
| Omstart mister prioriteringsstatus | Lav | Ikke lagre prioritet; rekonstruer restkø fra faktisk evidensstatus |
| Låst Excel-fil stanser batchen | Middels | Behold koordinatorens pasientvise feilutfall og fortsett med neste |

## Open Questions

- Ingen nødvendige før implementering. Standardvalget er at rapportene for den
  prioriterte gruppen genereres når gruppens databasesøk er ferdige og lagret.
