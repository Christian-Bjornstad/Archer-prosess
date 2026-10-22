# Tredjepartsprogramvare og data

VPM Tolknings egen kode og tilhørende originaldokumentasjon distribueres under
GNU GPL versjon 3. Se [LICENSE](LICENSE) for fullstendige vilkår.
Tredjepartskomponenter beholder sine egne lisenser og opphavsrettsmerknader.

## Python-avhengigheter

Wheel-filen for VPM Tolkning pakker ikke inn bibliotekene nedenfor; de
installeres separat av pakkebehandleren. Tabellen oppsummerer lisensene i
metadataene til de gjennomgåtte bibliotekversjonene. Fullstendige vilkår og
merknader følger de respektive pakkene. Også transitive avhengigheter har egne
lisensvilkår.

| Bibliotek | Lisens |
| --- | --- |
| PyQt6 | GPL-3.0-only, eller separat kommersiell lisens fra Riverbank |
| Qt, installert via PyQt6-Qt6 | LGPLv3; se medfølgende lisensfiler for komponentene |
| PyQt6-sip | BSD-2-Clause |
| keyring | MIT |
| pandas | BSD-3-Clause |
| openpyxl | MIT |
| Pillow | MIT-CMU |
| ReportLab | BSD |
| Requests | Apache-2.0 |
| websocket-client | Apache-2.0 |

PyQt har egne [lisensvilkår hos Riverbank](https://www.riverbankcomputing.com/software/pyqt).
Biblioteker, lisensfiler, opphavsrettsmerknader og eventuelle kildekodekrav må
tas med når komponentene faktisk pakkes inn i en installer eller annen
selvstendig distribusjon. Denne oversikten erstatter ikke disse filene.

## Eksterne tjenester og referansedata

COSMIC, OncoKB, Franklin, ClinVar og MTBP er eksterne datakilder med egne
tilgangs- og datavilkår. Prosjektets GPL-lisens gir ingen rett til å gjenbruke
eller distribuere materiale hentet fra disse tjenestene utover deres vilkår.

Referansearbeidsbøkene i `reference_lists/` er eksport av prosjektets eksisterende
regelsett. De er ikke et vedlagt WHO-publikasjonsverk eller en ny faglig
validering. Se [referanselistenes beskrivelse](reference_lists/README.md).
