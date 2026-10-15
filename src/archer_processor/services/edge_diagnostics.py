"""Read-only workstation checks; never starts Edge or changes managed policy."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeWorkstationDiagnostics:
    edge_executable: str
    executable_error: str
    policy_values: tuple[tuple[str, object, str], ...]
    policy_read_error: str
    proxy_environment_names: tuple[str, ...]

    @property
    def launch_problems(self) -> tuple[str, ...]:
        problems = []
        if self.policy_read_error:
            problems.append(
                "Managed Edge policy could not be read. Ask IT to confirm that "
                "RemoteDebuggingAllowed permits local debugging and UserDataDir "
                "does not override the dedicated evidence profiles. "
                f"Policy read error: {self.policy_read_error}"
            )
        for name, value, source in self.policy_values:
            if name == "RemoteDebuggingAllowed" and value == 0:
                problems.append(
                    f"RemoteDebuggingAllowed is disabled ({source}). Ask IT "
                    "whether local Edge evidence collection is permitted."
                )
            elif name == "UserDataDir" and value:
                # This mandatory policy wins over --user-data-dir. Refuse to
                # risk redirecting a launch into the user's ordinary browser.
                problems.append(
                    f"UserDataDir is managed ({source}) and overrides the app's "
                    "dedicated evidence profile. Ask IT for a configuration "
                    "that permits isolated Edge profiles."
                )
        return tuple(dict.fromkeys(problems))

    @property
    def launch_blocked(self) -> bool:
        return bool(self.executable_error or self.launch_problems)


def collect_edge_diagnostics() -> EdgeWorkstationDiagnostics:
    """Collect local installation/policy facts without opening any browser.

    Registry absence is different from unreadable policy. No policy values,
    proxy addresses, credentials, or browser profile contents are changed.
    This is a preflight, not proof that a site or Citrix connection works.
    """
    from archer_processor.services.edge_cdp import EdgeCdpError, find_edge_executable

    try:
        executable, executable_error = str(find_edge_executable()), ""
    except EdgeCdpError as exc:
        executable, executable_error = "", str(exc)
    try:
        policies = tuple(_read_edge_policy_values())
        policy_error = ""
    except (OSError, ImportError) as exc:
        policies, policy_error = (), f"{type(exc).__name__}: {exc}"
    proxy_names = tuple(sorted(
        key for key in os.environ
        if key.casefold() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    ))
    return EdgeWorkstationDiagnostics(
        executable, executable_error, policies, policy_error, proxy_names,
    )


def _read_edge_policy_values() -> list[tuple[str, object, str]]:
    if os.name != "nt":
        return []
    import winreg

    values: list[tuple[str, object, str]] = []
    # Check both registry views: 32-bit Python can run on a 64-bit Citrix host.
    for hive, label in (
        (winreg.HKEY_LOCAL_MACHINE, "HKLM"),
        (winreg.HKEY_CURRENT_USER, "HKCU"),
    ):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\Policies\Microsoft\Edge", 0, winreg.KEY_READ | view)
            except FileNotFoundError:
                continue
            with key:
                for name in ("RemoteDebuggingAllowed", "UserDataDir"):
                    try:
                        value, kind = winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        continue
                    expected_kind = winreg.REG_DWORD if name == "RemoteDebuggingAllowed" else winreg.REG_SZ
                    if kind != expected_kind:
                        raise OSError(f"Unexpected registry type for {name} in {label}")
                    entry = (name, value, label)
                    if entry not in values:
                        values.append(entry)
    return values
