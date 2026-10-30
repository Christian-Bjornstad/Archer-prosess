# Utvikling og bidrag

## Kjør fra kildekode

Åpne en terminal i en ren kildekopi og opprett et eget miljø:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\vpm-tolkning.exe
```

Kjør testene med samme Python-miljø:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Testene bruker syntetiske data og isolerte innstillinger. De små fixture-filene
ligger i `tests/fixtures/`. DIT-verdiene der er konstruerte test-IDer.
Tester skal ikke kreve kliniske filer, ekte kontoer eller kontakt med datakildene.

De to manuelle Edge-kontrollene bruker lokal, syntetisk HTML og separate
midlertidige nettleserprofiler:

```powershell
.\.venv\Scripts\python.exe scripts\verify_capture_locally.py
.\.venv\Scripts\python.exe scripts\verify_provider_recovery_dom.py
```

## Endringer

Hold feilrettinger avgrenset og legg til regresjonstester ved endret oppførsel.
Kontroller særlig variantidentitet, genomreferanse, lagring ved feil og
pasienttilordning i rapportene. Treff må aldri godtas bare fordi et gen eller
en proteinendring stemmer når en mer presis identitet motsier resultatet.

Bruk bare syntetiske demonstrasjonsdata i kode, tester, dokumentasjon og
feilrapporter. Oppdater brukerveiledningen når arbeidsflyten endres.
Ikke legg inn gamle arbeidsplaner, samtalelogger, kontoer, private filbaner
eller kliniske eksportfiler i repoet. Bidrag gis under prosjektets GPLv3-lisens.

## Bygg og distribusjon

Bygg wheel og kildepakke fra en ren kildekopi:

```powershell
.\.venv\Scripts\python.exe -m pip install build
.\.venv\Scripts\python.exe -m build
```

`dist/` får en wheel for installasjon og en `.tar.gz` med kildekode.
Kildepakken inkluderer tester, syntetiske fixtures og referanselister.
Wheel-filen inneholder appen, ikonene og lisensinformasjon; den installerer
avhengigheter separat. Den illustrerte brukerguiden kan deles som egne HTML-
og PDF-filer.

Kontroller innholdet i pakkene før deling og test installasjon i et nytt miljø.
Del den kontrollerte kildepakken sammen med wheel-filen og eventuelle
brukerdokumenter. En kopi eller ZIP av hele arbeidsmappen kan ta med lokale
pasientfiler, selv om de ignoreres av Git. Gamle opplysninger kan også finnes
i Git-historikken; sletting fra gjeldende kildekode fjerner dem ikke fra eldre
commits.

Hvis en egen Windows-installer senere lages, må også lisensene og kildekodekravene
for bibliotekene som følger med håndteres; se [tredjepartsoversikten](THIRD_PARTY_NOTICES.md).

## Kodestruktur

| Mappe | Ansvar |
| --- | --- |
| `src/archer_processor/core/` | Variantmodeller, filtrering og behandling |
| `src/archer_processor/gui/` | PyQt-grensesnitt og arbeidstråder |
| `src/archer_processor/io/` | TSV-import |
| `src/archer_processor/knowledge/` | Historiske varianttreff |
| `src/archer_processor/reports/` | Excel-arbeidsbøker og pasientvedlegg |
| `src/archer_processor/services/` | Datakilder, nettleser, innstillinger og gjenopptak |
| `tests/` | Regresjonstester med syntetiske data |
| `docs/` | Brukerguide og driftsveiledning |
