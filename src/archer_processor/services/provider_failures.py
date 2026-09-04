from __future__ import annotations

from enum import StrEnum


class ProviderFailureKind(StrEnum):
    NOT_FOUND = "not_found"
    LOGIN_REQUIRED = "login_required"
    LAYOUT_CHANGED = "layout_changed"
    AMBIGUOUS = "ambiguous"
    IDENTITY_MISMATCH = "identity_mismatch"
    TRANSIENT = "transient"


class ProviderLookupError(RuntimeError):
    def __init__(self, kind: ProviderFailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
