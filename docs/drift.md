# Lagring, innlogging og feilsøking

## Lokale filer

| Innhold | Plassering |
| --- | --- |
| Innstillinger | `%USERPROFILE%\.archer-prosess\config.json` |
| Edge-profiler | Som standard `%USERPROFILE%\.archer-prosess\browser_profiles`, eller mappen valgt i Innstillinger |
| Arbeidsbok, evidens og kjørelogger | Utområdet for den aktuelle analysen |
| Pasientvedlegg | `VEDLEGG_APP` ved siden av arbeidsboken |
| Lagrede passord | Operativsystemets legitimasjonslager via `keyring` |

Innstillingsfilen kan inneholde kontoopplysninger, lokale filbaner og eventuelle
API-nøkler. Edge-profiler inneholder blant annet innloggingsøkter. Logger,
evidensfiler og rapporter kan inneholde prøve-IDer, varianter og lokale baner.
Disse filene skal ikke legges i offentlige feilrapporter eller deles sammen med
programmet. Sikkerhetskopier arbeidsdata etter virksomhetens rutiner.

Velg en egnet lokal mappe for Edge-profilene hvis standardmappen synkroniseres
eller styres av virksomheten. Endring av profilmappe flytter ikke eksisterende
profiler; du må logge inn i den nye profilen. Den valgte plasseringen vises i
Innstillinger.

## Datatilgang

Databasesøk sender variantopplysninger til valgte kilder. MTBP mottar variantsett
med appgenererte rapport-IDer og den valgte krefttypen. Originale pasient- og
prøve-IDer skal ikke brukes som rapport-ID hos leverandørene. Opplysninger om
varianter kan likevel være sensitive og krever virksomhetens godkjente arbeidsflyt.

Appens lisens gir ingen tilgangsrett til COSMIC, OncoKB, Franklin, ClinVar eller
MTBP. Kontroller leverandørenes tilgangs- og datavilkår før bruk eller videre
deling av hentet materiale.

## Innlogging og første besøk

Lagre brukernavn og passord under **Innstillinger**. **Sjekk innlogging**
kontrollerer alle kildene og forsøker automatisk innlogging der en kilde ber
om det. **Sjekk** ved én database gjør det samme bare for denne kilden.
Appen gjenbruker en bekreftet økt uten å sende inn passord på nytt.

Status vises under hver kilde: **Innlogget**, **Ikke innlogget**,
**Ikke bekreftet** eller **Kunne ikke sjekke**. ClinVar viser **Offentlig tilgang**
når siden er bekreftet; den krever ikke innlogging. En sendt innloggingsforespørsel
er ikke tilstrekkelig til å vise **Innlogget**. Ved ekstra innloggingssteg eller
feil opplysninger brukes **Logg inn** for å fullføre manuelt.

Bruk **Logg inn** for den aktuelle kilden og fullfør innloggingen i vinduet
som åpnes. Appen gjenbruker økten i sin egen Edge-profil. Den vanlige
nettleserprofilen din er ikke den samme som appens profil.

Franklin kan vise valgfrie spørsmål ved første søk. Appen forsøker å hoppe over
slike spørsmål uten å finne på pasientopplysninger. Hvis en innloggingsside,
et tilgangskrav eller en ukjent dialog stopper søket, åpner du kilden manuelt
og fullfører det nødvendige steget før du prøver igjen.

## Vanlige problemer

| Problem | Handling |
| --- | --- |
| Excel-filen er åpen eller skrivebeskyttet | Lukk Excel eller gi filen skrivetilgang. Bruk **Prøv ventende lagring igjen**. Kontroller at lagringen fullføres før appen lukkes. |
| Treff vises, men dokumentasjonen er ufullstendig | Les detaljstatus og logg. Kontroller evidensbildet og prøv det uferdige oppslaget igjen. |
| Innlogging blir ikke gjenkjent | Kontroller valgt profilmappe og logg inn via appens **Logg inn**-handling. |
| Edge starter ikke | Kontroller at Edge er installert og at virksomhetens policy tillater appens fjernstyrte Edge-økt. Kontakt lokal IT ved policyblokkering. |
| Referanselisten avvises | Kontroller banen, arkoverskriftene og verdiene mot [formatbeskrivelsen](../reference_lists/README.md). |
| Et søk er avbrutt | Gjenåpne den behandlede Excel-filen og fortsett uferdige oppslag. Kontroller ventende lagring først. |

MTBP bruker midlertidige rapporter hos leverandøren. Appen rydder egne gamle
rapporter før en ny innsending. Den lokale arbeidsboken og evidensen er derfor
grunnlaget for gjenopptak; en gammel rapportlenke alene er ikke en sikkerhetskopi.

Ved feilrapportering: oppgi appversjon, feilmelding, kilde og reproduksjonstrinn.
Lag en liten syntetisk fil som viser problemet, og fjern kontoopplysninger,
pasientdata og private filbaner fra vedleggene.
