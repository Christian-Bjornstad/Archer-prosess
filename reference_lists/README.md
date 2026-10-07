# Redigerbare referanselister

Disse filene inneholder dagens innebygde innhold, uendret:

- **Artefaktliste.xlsx:** 51 regler, inkludert Artefakter v7. Arket `Artifacts`
  har `Gene`, `HGVSc`, `Artifact through AF` og `Reason` på rad 1.
- **WHO-drivergener.xlsx:** 54 gensymboler. Arket `WHO` har `Gen` på rad 1.

Kopier filene til en fast lokal mappe dere kan redigere. I appen velges de under
**Settings → Local files**, i feltene **Artefaktliste** og **WHO-drivergener**.
Trykk **Save Configuration**. Deretter kan filene oppdateres i Excel uten å
endre kode. Appen overskriver ikke referansefilene.

En valgt artefaktfil erstatter hele artefaktlisten ved neste behandling eller
gjenåpning. Den redigerbare tabellen i Innstillinger er da deaktivert, men dens
lagrede regler beholdes. Fjern filvalget for å bruke disse reglene igjen; en ny
installasjon bruker den hardkodede listen. WHO-filen erstatter hele genlisten;
uten filvalg brukes den hardkodede WHO-listen.

Legg til, endre eller slett datarader. Behold overskriftene på rad 1, og bruk
faste verdier fremfor formler. For artefakter er Gene og transkript/HGVSc
påkrevd. Duplikater etter at transkriptversjon er fjernet avvises. En tom
AF-grense betyr artefakt uansett AF; en angitt grense inkluderer lik verdi.
AF-celler er ekte Excel-prosenter: skriv `5,5%` for 5,5 prosent. Grensen kan
også være null. Terskelverdier og øvrige kliniske regler er ikke faglig endret.

Filene leses på nytt når en TSV behandles eller en analyse gjenåpnes.
WHO-gener leses på nytt ved skriving av arbeidsbøker og pasientvedlegg.
Gjeldende analyser endrer ikke artefaktbeslutninger midt i et søk; gjenåpne
arbeidsboken for å anvende endrede regler. En manglende, skadet eller ugyldig
valgt fil gir en tydelig feil, uten automatisk bytte til andre regler.

Listene er eksportert fra appens eksisterende katalog 7. oktober 2026.
De representerer ingen ny kontroll av WHO-retningslinjer eller laboratoriets
artefaktkatalog.
