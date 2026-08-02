from __future__ import annotations


SERVICE_PREFIX = "Archer Prosess"


class CredentialStoreError(RuntimeError):
    """Windows credential storage could not be read or updated."""


def get_saved_password(provider: str, username: str) -> str:
    if not username:
        return ""
    try:
        import keyring

        return keyring.get_password(f"{SERVICE_PREFIX}/{provider}", username) or ""
    except Exception as exc:
        raise CredentialStoreError(
            f"Could not read the saved {provider} password from Windows Credential Manager."
        ) from exc


def save_password(provider: str, username: str, password: str) -> None:
    if not username or not password:
        return
    try:
        import keyring

        keyring.set_password(f"{SERVICE_PREFIX}/{provider}", username, password)
    except Exception as exc:
        raise CredentialStoreError(
            f"Could not save the {provider} password in Windows Credential Manager."
        ) from exc
