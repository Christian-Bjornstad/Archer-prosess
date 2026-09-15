from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


_SENSITIVE_FIELD_PARTS = (
    "password",
    "secret",
    "token",
    "cookie",
    "credential",
    "session",
)


class RunJournal:
    """Thread-safe, append-only human and machine readable run log."""

    def __init__(
        self,
        directory: Path,
        *,
        run_mode: str,
        app_version: str,
        run_id: str,
    ) -> None:
        self.directory = directory
        self.run_mode = run_mode
        self.app_version = app_version
        self.run_id = run_id
        self.text_path = directory / f"{run_id}.log"
        self.jsonl_path = directory / f"{run_id}.jsonl"
        self.last_error = ""
        self._lock = threading.Lock()

    @classmethod
    def start(
        cls,
        directory: Path,
        *,
        run_mode: str,
        app_version: str,
    ) -> RunJournal:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        journal = cls(
            Path(directory),
            run_mode=run_mode,
            app_version=app_version,
            run_id=f"{timestamp}-{uuid4().hex[:8]}",
        )
        try:
            journal.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            journal.last_error = str(exc)
        return journal

    def record(self, message: str, **fields: Any) -> bool:
        now = datetime.now(timezone.utc)
        structured_fields = _structured_message_fields(str(message))
        safe_fields = {
            key: value
            for key, value in {**structured_fields, **fields}.items()
            if not any(part in key.casefold() for part in _SENSITIVE_FIELD_PARTS)
        }
        record = {
            "timestamp": now.isoformat(),
            "run_id": self.run_id,
            "app_version": self.app_version,
            "run_mode": self.run_mode,
            "message": str(message),
            **safe_fields,
        }
        text_line = f"[{now.astimezone().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        json_line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                with self.text_path.open("a", encoding="utf-8", newline="") as stream:
                    stream.write(text_line)
                with self.jsonl_path.open("a", encoding="utf-8", newline="") as stream:
                    stream.write(json_line)
            self.last_error = ""
            return True
        except OSError as exc:
            self.last_error = str(exc)
            return False

    def close(self) -> None:
        """Files are opened per event, so no handle remains to close."""


def _structured_message_fields(message: str) -> dict[str, str]:
    parts = [part.strip() for part in message.split("|")]
    if len(parts) < 2 or parts[0] not in {"RESULT", "SUMMARY", "PENDING"}:
        return {}
    extracted = {"event_type": parts[0]}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, value = (item.strip() for item in part.split("=", 1))
        if key and not any(term in key.casefold() for term in _SENSITIVE_FIELD_PARTS):
            extracted[key] = value
    return extracted
