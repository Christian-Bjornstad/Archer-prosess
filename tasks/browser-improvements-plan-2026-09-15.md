# Plan: Mer pålitelig nettsidekjøring på jobb-PC

Status: undersøkt og klar for gjennomføring senere. Ingen av oppgavene nedenfor
er implementert i denne planleggingsrunden.

## Rammer

- Alle databaseoppslag skal gå gjennom nettsider med brukerens egne kontoer,
  og rapportene skal ha skjermbilder. Ingen database-API-er.
- Bruk eksisterende Python/PyQt/Edge-oppsett uten nye programmer, tjenester,
  administratorrettigheter eller ekstra nettleserinstallasjoner.
- Behold Franklin-opptakene som fungerer, GRCh37-verifikasjon, pasientisolasjon
  og muligheten til å stoppe/gjenoppta.
- Planen for prioriterte pasienter i tasks/plan.md og tasks/todo.md har fortsatt
  en åpen manuell test. Den bevares; denne filen inneholder den nye oppgavelisten.

## Bekreftede funn og begrensninger

1. Loggutdraget har 500 logglinjer pluss `<br>`, starter ved pasient 18 og
   inkluderer en senere resume-runde. Det er ikke hele hovedkjøringen.
   gui/app.py setter setMaximumBlockCount(500), og _log skriver bare til
   visningen. Den første delen kan derfor ikke analyseres fra dette vedlegget.
2. MTBP _delete_mtbp_report bruker startswith(MTBP_REPORTS_URL) for å avgjøre
   om Reports List er åpen. En detaljrapport under /patients/... tilfredsstiller
   samme sjekk. Fravær av rapportlenken der kan gi feilaktig already_absent.
   Loggen viser already_absent fulgt av sletting av samme rapport ved neste
   pasient. Dette støtter hypotesen, men selve feilen trenger en reproduksjonstest.
3. MTBP _cleanup_stale_mtbp_reports prøver å slette alle synlige ARCHER-rapporter,
   også når det finnes ledig kapasitet. Den mottar ingen liste over rapporter
   som må beholdes for ufullstendig lokal innhenting.
4. _recover_mtbp_timeouts går gjennom varianter og åpner Reports List per variant.
   Flere varianter kan tilhøre samme analysis_id. Loggteksten teller varianter
   som «retained report(s)», så tidligere omtale av 11 rapporter var upresis:
   det var 11 variantposter til gjenoppretting, ikke nødvendigvis 11 rapporter.
5. Excel lagres synkront fra GUI etter hvert kilde-checkpoint. Writer lagrer
   direkte til målfilen. Faktisk skrivetid på K:-disken er ikke målt separat.
6. ClinVar-nettleserflyten bruker fortsatt E-utilities internt for å finne og
   verifisere treff før skjermbildet. Dette må erstattes for å oppfylle kravet
   om en fullstendig nettsidebasert arbeidsflyt.
7. MTBP sjekker kø/Reports List omtrent hvert 15. sekund; 60 sekunder er bare
   statusmeldingsintervallet. Det finnes ikke grunnlag for å love stor gevinst
   fra hyppigere polling.
8. Forrige rundes live-prober bekreftet OncoKB-terminaltilstander og COSMIC
   Mutation not found. Rettelsene er pushet på fix/run-diagnostics-recovery.
   Målinger på lokal PC er ikke et nytt benchmark for Citrix. Tidligere anslag
   om at hele resume-runden blir 2–3 minutter er ikke verifisert.

## Prioritert oppgaveliste

### 1. Beskytt MTBP-rapporter under gjenopptak — høyest prioritet

Omfang: browser_review.py og nettlesertester. Avhengigheter: ingen.

- [ ] Reproduser already_absent fra detaljrapport og krev bekreftet Reports List
  før fravær eller sletting kan konkluderes. Vent på at listen er ferdig lastet.
- [ ] Beskytt rapport-ID-er med ufullstendig innhenting på tvers av pasienter og
  omstart; slett bare rapporter som er bekreftet trygge å fjerne, ved behov.
- [ ] Verifiser at en delvis fanget rapport fortsatt kan gjenopptas etter at neste
  pasient er behandlet. Sletting skal bare skje etter kontrollert lokal lagring.

Kontroll: falsk fraværstilstand, full kapasitet, delvis rapport og omstart i
tests/test_browser_review.py; deretter én syntetisk MTBP-kjøring på jobb-PC.

### 2. Behold hele kjøreloggen — høy prioritet

Omfang: liten logging-modul, gui/app.py og tester. Avhengigheter: ingen.

- [ ] Skriv en full kjørelogg til godkjent resultatmappe uavhengig av de 500
  synlige linjene. Legg til «Åpne loggmappe»/kopiering av hele loggen.
- [ ] Gi hver kjøring en ID og logg appversjon, kjøremåte, kilde, pasientindeks,
  variantreferanse, resultat, feilstadium, forsøk og tid. Behold lesbar tekst;
  lag JSONL for analyse med standardbiblioteket. Ingen passord eller sesjonsdata.
- [ ] Vis avslutning med faktisk funnet, ikke funnet, manuell kontroll og uferdig;
  en ferdig kø betyr ikke at alle resultater er klare.

Kontroll: >500 hendelser, gjenstart, samtidige kilder og ikke-skrivbar loggmappe.
Loggsvikt skal varsles uten å miste allerede innhentet evidens.

### 3. Lagre hvert resultat og gjør Excel-skriving robust — høy prioritet

Omfang: deles i to små snitt. Avhengigheter: oppgave 2 for måling.

- [ ] Først: lagre alle utfall, inkludert timeout og ikke funnet, per variant/kilde
  før neste oppslag. Bruk eksisterende audit-format konsekvent med varighet og
  gjenopptakskontekst; ingen tap ved stopp midt i en database.
- [ ] Deretter: én seriell bakgrunnsskriver for Excel med eget datasnapshot;
  skriv til midlertidig fil og erstatt målfil først etter vellykket lagring.
  Bevar siste gode fil ved lås/nettverksbrudd og siste evidens i journalen.
- [ ] Mål antall og varighet av Excel-skrivinger før eventuell sammenslåing av
  nærliggende lagringsforespørsler. Rapportgenerering venter på korrekt versjon.

Kontroll: avbryt etter variant 2/5, åpne Excel-lås, simuler diskfeil, gjenlast
arbeidsbok og journal. Pause/stopp i GUI skal svare under lagring.

### 4. ClinVar via nettsiden hele veien — nødvendig for API-fri drift

Omfang: først lesende DOM-undersøkelse, deretter eget implementeringssnitt i
browser_review.py/variant_identity.py med tester. Avhengigheter: oppgave 2–3.

- [ ] Undersøk nettsidens HGVS-søk, resultatliste og variantkort på representative
  SNV-er, delesjoner, duplikasjoner og transkriptversjoner.
- [ ] Verifiser eksakt GRCh37-lokus og alleler fra nettsidens innhold før innhenting;
  avklar indelrepresentasjoner. Koordinattreff alene er utilstrekkelig.
- [ ] Fjern E-utilities fra aktiv nettleserflyt og test at den ikke kaller noen
  database-API. Uavklarte treff får konkret årsak og kontrollbehov.

Kontroll: positive/negative treff, feil allel på samme posisjon, flere treff,
ingen treff, treg side og verifiserte skjermbilder. Mål kostnaden i nettlesertid.

### 5. Bedre OncoKB-håndtering av transkriptavvik

Omfang: DOM-undersøkelse og eget snitt. Avhengigheter: oppgave 2 og 4s
identitetskontroller der de er relevante.

- [ ] Undersøk om nettsiden tilbyr søk med genomisk identitet som kan løse
  transkriptavvik uten API. Ikke anta at nettsiden støtter det.
- [ ] Hvis en dokumenterbar og identitetsverifisert nettlesersti finnes, ta
  skjermbilde av det riktige resultatet; aldri bytt referanseaminosyre på gjetning.
- [ ] Ellers vis «Transkriptavvik – manuell kontroll» med konkret forklaring og
  lenke; skill dette fra «genet finnes ikke», uten gjentatte automatiske forsøk.

Kontroll: NF1-/KDM6A-typen fra forrige probe, et gyldig treff og et ukjent gen.

### 6. Fjern unødvendig venting — etter korrekthet og målinger

Omfang: separate små snitt i browser_review.py. Avhengigheter: oppgave 1–3.

- [ ] Behandle MTBP-gjenoppretting samlet per unik rapport-ID: én åpning og
  innhenting, deretter fordeling til variantene. Logg rapporter og varianter hver for seg.
- [ ] Hopp over COSMIC-nettleserstart og nettbuffer når hele omfanget mangler
  COSMIC-ID; ingen venting etter lokale hopp-over-resultater.
- [ ] Undersøk trygg gjenbruk av identiske kildeoppslag i samme kjøring med
  eksakt identitetsnøkkel, uavhengige evidensobjekter og stabile bildefiler.
  MTBP-pasientrapporter skal ikke gjenbrukes mellom pasienter.

Kontroll: samme variant hos to pasienter, forskjellige alleler, manglende bilder,
cache-treff og at et endret resultat aldri påvirker en annen pasient.

### 7. Mål på jobb-PC før mer parallellitet

Avhengigheter: 1–6 gjennomført i relevante snitt.

- [ ] Kjør et lite representativt datasett: én variant, flere varianter, gjentatte
  varianter, transkriptavvik og et gjenopptak. Registrer kilde-/lagringstid og RAM.
- [ ] Sammenlign med tre eksisterende nettleserbaner. Undersøk gjenbruk av Edge
  per kilde gjennom kjøringen før det vurderes flere samtidige prosesser.
- [ ] Godta endringer først når bilder, identitet, stopp/gjenopptak og ressursbruk
  er verifisert på jobb-PC. Tilby redusert samtidighet ved ressursproblemer.

## Anbefalt første gjennomføringsrunde

Oppgave 1 og 2 først, så 3. Deretter oppgave 4 for full API-fri arbeidsflyt.
Oppgave 5–7 følger som egne kontrollerbare endringer. Hvert snitt får relevante
regresjonstester, gjennomgang og en egen commit; ingen global reduksjon av alle
buffere. Full testpakke kjøres etter samlede endringer.

## Hva som trengs fra brukeren senere

Ingen ny innlogging er nødvendig for denne planen. Under live-verifikasjon
trengs tilgang til jobb-PC/Citrix sin Edge og eventuelt fornyet innlogging hvis
sesjonen har utløpt. Første arbeidsplass-test bør inkludere en syntetisk MTBP-
rapport og en låst Excel-fil. Eksisterende manuell prioritetstest kan tas samtidig.
