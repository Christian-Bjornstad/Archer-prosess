# Edge CDP med dynamisk port — guide for andre eMolPat-apper

**Dato:** 7. september 2026
**Utløser:** Edge CDP-funksjonen i VPM sluttet å fungere. VPM/andre eMolPat-apper som kjører `subprocess.Popen(["msedge.exe", "--remote-debugging-port=<fast port>"])` treffer det samme problemet på den administrerte Edge-installasjonen på Windows Server 2022.
**Løsning:** la Edge selv velge port, og les den faktiske porten fra `DevToolsActivePort`.
**Status:** Verifisert mot Microsoft Edge 151.0.4129.101 på denne maskinen og 258 enhetstester passerer (`tests/test_edge_cdp.py`).
**Sammendrag av feilsøkingen:** se `Edge_CDP_feilsoking_Python_Felles.md`.

---

## Rotårsaken

Den vanlige fremgangsmåten for å starte Edge med DevTools-støtte er:

1. Reservere en ledig port i Python:

   ```python
   import socket
   with socket.socket() as reservation:
       reservation.bind(("127.0.0.1", 0))
       requested_port = reservation.getsockname()[1]
   ```

2. Starte Edge med den reserverte porten:

   ```python
   subprocess.Popen([msedge, f"--remote-debugging-port={requested_port}", ...])
   ```

3. Koble seg til `http://127.0.0.1:{requested_port}/json/version`.

Problemet: i **administrerte Edge-installasjoner** (og særlig når Edge kjører med en prosess-broker) kan Edge ignorere den forespurte porten og lytte på en helt annen. Diagnostikken fortsetter dermed å teste feil port, og programmet rapporterer at CDP ikke er tilgjengelig — selv om CDP faktisk svarer, bare på en annen port.

Eksempel fra en feilet kjøring på denne maskinen:

```
Requested port: 25324
Faktisk CDP-port: 25303
Testet port: 25324     ← testen traff porten ingen lyttet på
```

Diagnoseskriptet som antar at Edge bruker den forespurte porten, får dermed `TimeoutError`. Forsøker man derimot porten Edge faktisk brukte etter at Edge er avsluttet, får man `ConnectionRefusedError` fordi porten da er lukket — det betyr ikke at porten var feil mens Edge kjørte.

---

## Den anbefalte arkitekturen

```
Start Edge med port 0
        ↓
Vent på DevToolsActivePort i profilen
        ↓
Les faktisk port
        ↓
Kontroller /json/version på den porten
        ↓
Hent webSocketDebuggerUrl fra svaret
        ↓
Koble klienten til den WebSocket-adressen
```

Dette fjerner enhver antagelse om hvilken port Edge ender med å bruke, og fungerer uavhengig av om brokeren overtar kommandoen eller ikke.

### Hvorfor port 0 er trygt å sende

`--remote-debugging-port=0` ber Edge om å velge en ledig port selv, på samme måte som `socket.bind(("127.0.0.1", 0))`. Edge skriver den valgte porten som første linje i `<user-data-dir>/DevToolsActivePort`. Det er den eneste pålitelige kilden til porten.

---

## Hva som må endres i en eksisterende CDP-integrasjon

1. **Ikke reserver en port i Python før Edge starter.** Kall `socket.bind` kun hvis du trenger porten til en egen HTTP-server; ikke for å sende den til Edge.
2. **Send `--remote-debugging-port=0` i stedet for et fast tall.**
3. **Fjern `--remote-allow-origins=...`.** Den kan ikke settes når porten er dynamisk (Edge kjenner den ikke før etter oppstart).
4. **Ikke send en `Origin`-header i WebSocket-håndtrykket.** Edge 111+ avviser enhver `Origin` som ikke er allow-listet med `--remote-allow-origins`. `websocket-client` syntetiserer som default en `Origin: http://127.0.0.1:<port>` fra URL-en, noe som alltid vil avvises når porten er dynamisk. Sett `suppress_origin=True` (Puppeteer/Playwright gjør det samme).
5. **Vent på `DevToolsActivePort`-filen etter at Edge er startet**, og les porten derfra.
6. **Kall `/json/version` på den porten før WebSocket-tilkoblingen**, slik at du får en fungerende `webSocketDebuggerUrl` å koble til.
7. **Bruk `webSocketDebuggerUrl` fra `/json/version`**, ikke en URL du setter sammen manuelt. Da tar du hensyn til query string og evt. fremtidige endringer.

---

## Drop-in-modul: `start_edge_cdp.py`

Denne filen er en selvstendig kopi av VPMs nåværende implementasjon. Den er testet og fungerer. Du kan kopiere den inn i andre apper, men merk:

- Den bruker `websocket-client` (allerede et eMolPat-krav via `pyproject.toml`).
- Den bruker `urllib.request` med en eksplisitt `ProxyHandler({})` slik at Citrix/enterprise-proxy ikke rutes til `127.0.0.1` (se eksisterende kommentarer i VPM).
- Den håndterer bare ett kall. Kall igjen for hver gang du trenger en ny profil (f.eks. mellom pasienter).

```python
"""Start Microsoft Edge with DevTools Protocol on a dynamic port.

This is the recommended way to launch the organisation-managed Edge
installation for browser evidence collection: it does not pre-reserve a
port, and it always talks to the port Edge itself announced via
DevToolsActivePort.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class EdgeCdpError(RuntimeError):
    """Microsoft Edge or its local DevTools connection failed."""


def find_edge_executable() -> Path:
    """Find the installed, organisation-managed Microsoft Edge executable."""
    discovered = shutil.which("msedge")
    candidates = [Path(discovered)] if discovered else []
    import os
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates.append(
                Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise EdgeCdpError(
        "Microsoft Edge was not found. Install or ask IT to expose the managed "
        "Edge installation before using browser evidence collection."
    )


def _http_json(url: str, *, timeout: float = 3) -> Any:
    """Call a local DevTools HTTP endpoint without using the enterprise proxy."""
    request = urllib.request.Request(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise EdgeCdpError(f"Edge DevTools endpoint failed: {url}: {exc}") from exc


def _read_devtools_active_port(port_file: Path) -> int:
    """Read the port Edge itself announced for this profile.

    Edge writes the first free port it chose (with --remote-debugging-port=0)
    as the first line of DevToolsActivePort inside the user-data-dir. This is
    the only reliable source: the managed Edge broker may ignore a pre-reserved
    port request entirely.
    """
    lines = port_file.read_text(encoding="utf-8").splitlines()
    port = int(lines[0])
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid DevTools port: {port}")
    return port


def start_edge_cdp(
    edge_path: str | Path,
    profile_path: str | Path,
    *,
    viewport: dict[str, int] | None = None,
    background: bool = False,
    accept_downloads: bool = True,
    download_directory: Path | None = None,
    timeout: float = 20,
) -> tuple[subprocess.Popen, str, str]:
    """Start Edge, wait for its CDP server, and return the endpoint.

    Returns ``(process, http_endpoint, websocket_url)``:

    - ``process`` is the Popen for the Edge process. Terminate it when done.
    - ``http_endpoint`` is the base URL of the DevTools HTTP API, e.g.
      ``http://127.0.0.1:25687``. Use it for ``/json/list``, ``/json/new`` etc.
    - ``websocket_url`` is the ``webSocketDebuggerUrl`` returned by
      ``/json/version``. Connect to it with ``suppress_origin=True``.

    Edge is launched with an isolated profile in ``profile_path`` and a dynamic
    DevTools port. The caller is responsible for ``process.terminate()`` and
    (if needed) removing ``profile_path`` afterwards.
    """
    edge = Path(edge_path)
    profile = Path(profile_path)
    profile.mkdir(parents=True, exist_ok=True)

    viewport = viewport or {"width": 1440, "height": 1000}
    port_file = profile / "DevToolsActivePort"
    try:
        port_file.unlink()
    except FileNotFoundError:
        pass

    arguments = [
        str(edge),
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile.resolve()}",
        f"--window-size={int(viewport['width'])},{int(viewport['height'])}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "--disable-background-mode",
        "--disable-features=msEdgeStartupBoost",
        "--disable-session-crashed-bubble",
        "about:blank",
    ]
    if background:
        arguments[1:1] = [
            "--start-minimized",
            "--window-position=-32000,-32000",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]

    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise EdgeCdpError(
                f"Microsoft Edge exited (code {process.returncode}) before "
                f"DevTools was reachable. Profile: {profile}"
            )
        if port_file.exists():
            try:
                port = _read_devtools_active_port(port_file)
            except (OSError, ValueError, IndexError):
                time.sleep(0.2)
                continue
            endpoint = f"http://127.0.0.1:{port}"
            try:
                version = _http_json(f"{endpoint}/json/version", timeout=1)
            except EdgeCdpError:
                time.sleep(0.2)
                continue
            websocket_url = str(version["webSocketDebuggerUrl"])
            if accept_downloads and download_directory is not None:
                download_directory.mkdir(parents=True, exist_ok=True)
                # The download behaviour is set later by the caller once a
                # page is available; it needs a live WebSocket session.
            return process, endpoint, websocket_url
        time.sleep(0.2)

    process.terminate()
    raise EdgeCdpError(
        f"Edge did not expose a reachable DevTools endpoint within {timeout}s. "
        f"Profile: {profile}"
    )


def open_cdp_websocket(websocket_url: str) -> Any:
    """Open a CDP WebSocket without sending an Origin header.

    ``suppress_origin=True`` is required: websocket-client would otherwise
    synthesise ``Origin: http://127.0.0.1:<port>`` from the URL host, and
    Edge 111+ rejects any Origin not allow-listed with
    ``--remote-allow-origins``. That flag cannot be set at launch time when
    the port is chosen dynamically. Origin-less local clients remain
    accepted.
    """
    import websocket
    from urllib.parse import urlsplit

    address = urlsplit(websocket_url)
    if address.scheme != "ws" or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise EdgeCdpError("DevTools must use a local loopback WebSocket.")
    transport = socket.create_connection(
        (address.hostname, address.port or 80), timeout=10
    )
    try:
        return websocket.create_connection(
            websocket_url,
            timeout=10,
            suppress_origin=True,
            enable_multithread=False,
            socket=transport,
        )
    except Exception:
        transport.close()
        raise


# Example usage
if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory(prefix="edge-cdp-") as tmp:
        process, endpoint, websocket_url = start_edge_cdp(
            find_edge_executable(),
            tmp,
            background=True,
        )
        try:
            print("HTTP endpoint:", endpoint)
            print("WebSocket URL:", websocket_url)
            ws = open_cdp_websocket(websocket_url)
            try:
                ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
                response = json.loads(ws.recv())
                print("Browser version:", response["result"].get("product"))
            finally:
                ws.close()
        finally:
            process.terminate()
            process.wait(timeout=5)
```

---

## Når du porterer dette inn i en annen app

| Steg | Hva | Hvorfor |
|---|---|---|
| 1 | Erstatt `socket.bind(...) → --remote-debugging-port=<tall>` med `--remote-debugging-port=0`. | Fjern rotårsaken. |
| 2 | Slett evt. eksisterende `--remote-allow-origins=...` fra Edge-argumentene. | Kan ikke settes når porten er dynamisk. |
| 3 | Etter at Edge er startet, les porten fra `<user-data-dir>/DevToolsActivePort` (første linje). | Det er den eneste pålitelige kilden. |
| 4 | Kall `GET http://127.0.0.1:<port>/json/version` og bruk `webSocketDebuggerUrl` fra svaret. | Edge kan utvide URL-formatet senere. |
| 5 | Sett `suppress_origin=True` på alle `websocket.create_connection`-kall. | Ellers avviser Edge 111+ håndtrykket med 403. |
| 6 | Husk å vente på at `DevToolsActivePort` finnes før du kaller `/json/version`. Edge kan bruke 1–3 sekunder. | Ellers får du `ConnectionRefusedError`. |
| 7 | Hvis du bruker en egen `Browser.setDownloadBehavior`, trenger du en åpen WebSocket-side til å sende kommandoen. `start_edge_cdp` returnerer den bare. | Gjør det etter `open_cdp_websocket`. |
| 8 | Rydd opp `profile_path` mellom kjøringer, men aldri mens Edge fortsatt holder håndtak i den. | Venter 0,5 s etter `process.terminate()` hjelper mot falske "profile in use"-feil. |

---

## Detaljert trinn-for-trinn (for en utvikler som aldri har gjort dette før)

1. **Finn Edge.** Kall `find_edge_executable()` fra drop-in-modulen. Den sjekker `PATH`, `Program Files (x86)`, `Program Files` og `LOCALAPPDATA` i den rekkefølgen. Hvis Edge er installert et annet sted (f.eks. `C:\Program Files\Edge\Application\msedge.exe`), kan du sende stien eksplisitt.

2. **Opprett en isolert profil i en midlertidig mappe.** Dette er viktig fordi administrerte Edge-profiler kan være delt mellom brukere, og flere samtidige CDP-økter i samme profil skaper portkonflikter. Eksempel:

   ```python
   from pathlib import Path
   import tempfile
   profile = Path(tempfile.mkdtemp(prefix="my-app-edge-cdp-"))
   ```

3. **Slett en evt. gammel `DevToolsActivePort` før oppstart.** Hvis profilen ble gjenbrukt, kan den inneholde en gammel port som ikke lenger er gyldig.

   ```python
   (profile / "DevToolsActivePort").unlink(missing_ok=True)
   ```

4. **Start Edge med port 0.** Bruk drop-in-modulen eller bygg argumentene selv:

   ```python
   process, endpoint, websocket_url = start_edge_cdp(
       find_edge_executable(),
       profile,
   )
   ```

5. **Koble til WebSocket.** Edge returnerer en `webSocketDebuggerUrl` i `/json/version`. Send den rett inn i `open_cdp_websocket()`:

   ```python
   ws = open_cdp_websocket(websocket_url)
   ```

6. **Utsted CDP-kommandoer.** Eksempel for å åpne en ny fane:

   ```python
   import json
   ws.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": "https://example.org"}}))
   print(json.loads(ws.recv()))
   ```

7. **Avslutt.** Kall `process.terminate()` og vent kort, så slett profilen:

   ```python
   process.terminate()
   try:
       process.wait(timeout=5)
   except subprocess.TimeoutExpired:
       process.kill()
   shutil.rmtree(profile, ignore_errors=True)
   ```

---

## Ting som feiler og hva de betyr

| Symptom | Betyr sannsynligvis | Løsning |
|---|---|---|
| `TimeoutError` / `ConnectionRefusedError` mot porten Edge ble bedt om | Edge bruker en annen port enn den forespurte | **Dette er bugen denne guiden løser.** Bruk port 0 og les `DevToolsActivePort`. |
| `FileNotFoundError: DevToolsActivePort` | Edge er ikke startet ennå, eller `--remote-debugging-port=0` mangler | Sjekk argumentene, vent lenger |
| `Handshake status 403 Forbidden` mot WebSocket | Origin-header ble sendt | Sett `suppress_origin=True`. Bekreft at du fjernet `--remote-allow-origins`. |
| `/json/version` returnerer 404 | Edge startet CDP på en annen host/port enn det du tester | Les porten fra `DevToolsActivePort` på nytt, ikke fra argumentene. |
| `ConnectionRefusedError` *etter* at Edge er avsluttet | Porten ble lukket da Edge ble drept | Ikke test porten etter at Edge er ferdig. Det er normalt. |
| `Edge exited (code 0) before ...` | Edge-prosessen avsluttet før CDP var klar | Sjekk `vpm-edge-startup.log` (eller tilsvarende) i profilen. Kan skyldes en `Browser.setDownloadBehavior` som krasjer Edge. |

---

## Hvorfor en forhåndsvalgt port er mindre robust

En vanlig idé er:

```python
import socket
with socket.socket() as reservation:
    reservation.bind(("127.0.0.1", 0))
    requested_port = reservation.getsockname()[1]
subprocess.Popen(["msedge.exe", f"--remote-debugging-port={requested_port}"])
```

Problemet er at det finnes et tidsvindu mellom `socket.close()` og Edge sin `bind()` der en annen prosess kan kapre porten. Og i administrerte Edge-installasjoner kan brokeren overta kommandoen og ignorere forespørselen helt. Med dynamisk portvalg forsvinner begge problemene.

---

## Hva denne endringen IKKE løser

- **Hvis Edge ikke er installert i det hele tatt:** gi tydelig feilmelding og be IT om å eksponere den administrerte installasjonen. Se `find_edge_executable()`.
- **Hvis DevTools er deaktivert av policy:** `Browser.setDownloadBehavior` og de fleste CDP-kall feiler. Edge-policyen `DeveloperToolsAvailability=2` blokkerer dette. Løsningen er en policyendring, ikke kode.
- **Hvis en annen Edge-prosess allerede holder en port:** ikke relevant her, siden port 0 gjør at Edge velger en ledig port. Men hvis du ser `bind: address already in use` i `DevToolsActivePort`, har noe annet låst den porten.

---

## Filreferanser

- Feilsøkingsnotat med rådata: `Edge_CDP_feilsoking_Python_Felles.md`
- Faktisk kildekode i VPM: `src/archer_processor/services/edge_cdp.py`
- Tester: `tests/test_edge_cdp.py`
- Commit-hash: se `git log -- src/archer_processor/services/edge_cdp.py`

## Referanser

- Microsoft Learn: Microsoft Edge DevTools Protocol
  https://learn.microsoft.com/en-us/microsoft-edge/devtools/protocol/
- Microsoft Learn: Edge-policyen `DeveloperToolsAvailability`
  https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/developertoolsavailability
- `websocket-client`: `suppress_origin=True` (Playwright/Puppeteer bruker samme triks)
