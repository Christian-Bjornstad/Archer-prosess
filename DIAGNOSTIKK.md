# Edge/Citrix: test lokalforbindelsen

Denne grenen endrer ikke databasesok eller eksisterende nettleserprofiler.

1. Hent grenen `diagnostics/citrix-edge-connection` i appmappen i Citrix.
2. Ikke start en databasekjoring samtidig med testen.
3. Dobbeltklikk **diagnose-edge.cmd**. Bruk samme Windows/Citrix-bruker som appen.
4. Vent til begge testene er ferdige (inntil omtrent tre minutter).
5. To rapporter apnes i Notisblokk. Del begge, og eventuelle feil i CMD-vinduet.

Testen bruker Python fra `.venv` hvis den finnes, ellers `py -3`.
Ingen ekstra Python-pakker installeres. CMD-filen stotter ogsa UNC/delte mapper.

## Hva blir malt?

- Python-versjon, operativsystem og hvilke proxyvariabler som finnes (ikke verdiene).
- TCP, direkte HTTP og HTTP med samme proxyfrie urllib-metode som appen mot
  en midlertidig lokal Python-server.
- De samme testene mot Edge: forst en valgt port som i appen, deretter en
  port valgt av Edge selv. Begge bruker nye, separate profiler under TEMP.
- Oppstartstid, Edge-versjon nar tilgjengelig, prosessavslutning og Edges oppstartslogg.

## Hvordan tolker vi resultatet?

- Lokal Python-server feiler: undersok lokal kommunikasjon i Citrix/Python-miljoet.
- Python-server virker, begge Edge-tester feiler: undersok Edge/prosessoppstart,
  lokal filtrering og servermiljoet med IT. Dette alene beviser ikke policyblokkering.
- Direkte HTTP virker, urllib feiler: forskjell i HTTP-klientbanen.
- Bare Edge-valgt port virker: undersok fast port/oppstartsforlopet i appen.
- Begge virker: undersok eksisterende profil, samtidige sesjoner og forskjeller
  mellom testoppstart og appoppstart. Ikke slett eksisterende profiler som et forste tiltak.
- Oppstart tar over 20 sekunder: appens kortere oppstartsvindu kan vaere relevant.

Rapportene inneholder lokale filstier og dermed mulig Windows-brukernavn.
De inneholder ikke pasientdata eller cookies. Se gjennom dem for deling.
Testfiler beholdes i `%TEMP%\vpm-edge-diagnostic-*`; eksisterende profiler flyttes
eller slettes aldri. Kun testens egen Edge-prosess avsluttes. Hvis et tomt
testvindu blir igjen, lukk det manuelt. Ingen sikkerhetsinnstillinger endres.

Dette er diagnostikk, ikke en bekreftet rettelse. Ingen nettsteder forespores av
testskriptet; Edge kan fortsatt utfore sine vanlige bakgrunnsforesporsler.
