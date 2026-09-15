# Plan: Mer pålitelig nettsidekjøring på jobb-PC

Status: implementering pågår på `fix/run-diagnostics-recovery`. Oppgavene krysses
av først etter automatisert verifikasjon; jobb-PC-kontroller står åpne til de er kjørt.

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

## Tillegg: målrettede reserve-søk, undersøkt 15. september 2026

### Hva researchen faktisk bekrefter

- OncoKB dokumenterer en **nettsiderute**, ikke bare et API, på
  `/hgvsg/{genomisk-HGVS}?refGenome=GRCh37`. Dette gir en konkret kandidat
  for transkriptavvik. Dokumentert eksempel er
  `https://www.oncokb.org/hgvsg/7:g.140453136A%3ET?refGenome=GRCh37`.
  Ruten er dokumentert, men avvikstilfellene våre er ikke live-verifisert via
  denne ruten ennå. [OncoKBs offisielle FAQ](https://faq.oncokb.org/technical).
- ClinVar dokumenterer nettsidesøk med cDNA og genomisk område filtrert med
  `[chrpos37]`. Dette kan brukes til å finne kandidater uten E-utilities;
  søket alene beviser ikke at allelene er riktige.
  [NCBIs lenkeveiledning](https://www.ncbi.nlm.nih.gov/clinvar/docs/linking/).
- NCBI forklarer at indeler kan ha forskjellige posisjonsrepresentasjoner.
  Ulik råposisjon er derfor ikke alltid ulik variant, men lik posisjon er
  heller ikke nok til å godkjenne et treff.
  [ClinVar FAQ](https://www.ncbi.nlm.nih.gov/clinvar/docs/faq/).
- Koden har allerede Franklin-reserve fra gen+cDNA til genomiske alleler.
  MTBP erstatter bare uttrykk nettsiden eksplisitt avviser, med eksisterende
  GRCh37/HGVS-konvertering. Behold disse prinsippene; ikke bygg dem om samlet.
- COSMIC begrenses etter brukerens krav til COSMIC-ID-er fra input.
  Flere oppgitte ID-er kan prøves i rekkefølge; ingen gen-, protein- eller
  koordinatsøk som reserve. Ikke konstruer nye ID-er.

### Søkeoppskrift per kilde

| Kilde | Første søk | Tillatt reserve | Når vi stopper |
| --- | --- | --- | --- |
| Franklin | Behold dagens gen+cDNA | Dagens eksakte genomiske GRCh37-uttrykk fra input | Verifisert variant med nødvendige bilder, eller uttømt kandidatliste |
| COSMIC | Første unike oppgitte COSMIC-ID | Neste unike oppgitte COSMIC-ID ved manglende/feil treff | Verifisert ID/GRCh37-treff; ingen ID gir lokalt «ikke aktuelt» |
| ClinVar | Transkript-HGVS fra input når tilgjengelig | Ett presist GRCh37-lokussøk; gjennomgå kandidatkort og alleler | Eksakt identitet; ellers «ikke funnet» eller «manuell kontroll» |
| OncoKB | Gen+protein som i dag | Dokumentert HGVSg-nettsiderute med GRCh37 ved transkriptavvik/variant uten treff | Riktig variant; ellers konkret kontrollårsak, ikke gjettet protein |
| MTBP | Dagens transkript-/variantuttrykk i pasientbatch | Genomisk HGVS kun for eksplisitt avviste uttrykk | Akseptert rapport gjenopptas; ikke opprett ny rapport ved vanlig venting |

Manglende felt gjør at kandidaten utelates. ClinVar kan starte med lokussøk
når transkriptet mangler. OncoKBs genomiske reserve aktiveres først etter
live-test av normal SNV, transkriptavvik og indel. Et ukjent gen skal ikke
utløse en kjede av gjentatte proteinforsøk. Et genkort eller generelt
«truncating mutations»-kort må ikke fremstilles som et eksakt varianttreff.

### Felles stoppregler og tidsbudsjett

- Skill **ny søkemåte** fra **nytt forsøk på samme søk** i kode og logg.
  Dedupliser kandidater, og husk hva som allerede er forsøkt ved gjenopptak.
- Foreslått startgrense: høyst to ulike søkemåter per kilde/variant;
  COSMIC begrenses i stedet til unike oppgitte ID-er. Høyst ett ekstra
  nettverksforsøk totalt per kilde/variant, ikke ett i hvert nestet lag.
  Dette er en implementeringsregel som skal testes, ikke en endring gjort nå.
- Ikke bytt søkeuttrykk ved utløpt innlogging, kvote, rate limit eller ødelagt
  sidelayout. Pause den berørte kilden og behold resten av arbeidet.
- Timeout før resultatvisningen er lastet er ikke «ikke funnet». Ved
  forbigående feil kan samme søk prøves én gang. Reserve-søk brukes når
  søkemåten/identiteten er problemet, ikke når tjenesten er utilgjengelig.
- Sett én samlet tidsfrist rundt alle oppslagstrinn for en variant/kilde,
  slik at underliggende ventinger ikke nullstiller budsjettet. Fastsett
  sekunder fra jobb-PC-målinger. MTBP-serveranalyse har eget rapportbudsjett,
  ikke samme korte oppslagsfrist som de andre kildene.
- To påfølgende kildeomfattende driftsfeil er et foreslått signal for å pause
  kilden. Vanlige «ikke funnet» skal aldri telle som driftsfeil.
- «Gjenoppta uferdige» skal ikke automatisk kjøre uttømte søk igjen.
  Et separat eksplisitt nytt forsøk kan nullstille søkehistorikken.

### Identitet og skjermbilder er godkjenningskravet

- Registrer original input, faktisk søkeuttrykk, valgt genomversjon,
  returnert variant/transkript, kontrollgrunnlag og bildesti.
- Verifiser mot variantkortets relevante felt, ikke et tilfeldig treff på
  samme tekst et sted i hele siden. Gen+cDNA uten transkript er ikke i seg
  selv bevis på eksakt transkriptidentitet.
- Franklin-koden merker i dag genomiske koordinater som GRCh37 i parseren.
  En senere kontrollendring må knytte dette til bekreftet hg19-modus, og
  kontrollere motstridende identitetsfelt. Gjør dette som eget testet snitt,
  uten samtidig omskriving av klikk-/opptakssekvensen.
- For indeler: bruk eksisterende testet representasjonskonvertering der
  gyldig. Ikke flytt koordinater, bytt strand eller oversett transkript på
  gjetning. Tvetydig ekvivalens sendes til manuell kontroll.
- Behold fungerende Franklin-buffer. Et faneklikk alene er ikke ferdig
  lasting: aktiv fane og det faktiske klassifikasjonspanelet må stemme.
  Ved bildefeil gjenopptas opptaket av bekreftet variant; ikke start ny
  søkekjede. Ikke krev at to bildefiler alltid har ulike hasher som eneste test.
- Alle påkrevde bilder må finnes og kunne leses før resultatet er komplett.
  «Funnet, bilde mangler» er uferdig, ikke et ferdig positivt resultat.

### Gjennomføring i små, kontrollerbare snitt

1. Oppgave 1–3 nedenfor: beskytt rapporter, full logg og varig resultatlagring.
2. Test søkehistorikk og felles statusregler uten å endre Franklin-opptak.
   Bruk små kildespesifikke kandidatlister og felles forsøksregistrering;
   ingen stor ny generell nettleserplattform.
3. OncoKB: lesende live-test av HGVSg-ruten med offentlig eksempel og våre
   avvikstyper. Deretter egen implementeringscommit dersom identiteten kan
   bekreftes. Ellers behold manuell kontroll for denne reserveveien.
4. ClinVar: nettsidebasert kandidatsøk og kortverifikasjon, deretter fjern
   aktive E-utilities-kall. Test at applikasjonen ikke gjør direkte
   database-API-kall; nettsidenes egen interne trafikk er ikke en ny integrasjon.
5. COSMIC: kun ID-liste, duplikatfjerning og raskt hopp over manglende ID.
   MTBP: test eksplisitt avvisning og én reserve uten doble rapporter.
6. Mål på jobb-PC før endring av buffere eller antall nettleserbaner.

Hvert snitt skal ha regresjonstester for første-søk-treff, reserve-treff,
begge uten treff, feil allel, feil genomversjon, login/timeout, manglende bilde
og avbrudd/gjenopptak. Legg til tre særlige negative tester: COSMIC uten ID
skal aldri navigere; OncoKB-transkriptavvik skal aldri «rettes» ved å bytte
aminosyre; MTBP-timeout skal ikke opprette en ny pasientrapport.

En samlet statustabell må brukes av GUI, audit og resume: ferdig med bilder,
ikke funnet etter fullført søk, ikke aktuelt, manuell kontroll og uferdig
drifts-/opptaksfeil. Ukjent status skal ikke stilltiende regnes som ferdig.

### Måling og godkjenning før utrulling

Kjør samme godkjente datasett før/etter og noter median/p95 per kilde og
variant (p95 først når antallet er meningsfullt), total pasienttid,
reserve-søkenes ekstra treff og tidskostnad, antall navigasjoner og andel
komplette bilder. Ingen påvist feilidentitet eller feil fane godtas i
testsettet. Hastighetsgevinst alene er ikke et godkjenningskriterium.
Bruk ett avgrenset kildesnitt om gangen; ved regresjon trekkes dette snittet
tilbake uten å reversere de fungerende Franklin-opptakene. En eventuell
tilbakerulling av ClinVar må ikke aktivere API-flyten igjen: deaktiver heller
kilden midlertidig og merk den uferdig.

## Opprinnelige arbeidsoppgaver, med oppdatert OncoKB-funn

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

- [ ] Live-verifiser den nå dokumenterte /hgvsg/-nettsideruten med GRCh37
  mot våre transkriptavvik. Se research og testkrav ovenfor.
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
