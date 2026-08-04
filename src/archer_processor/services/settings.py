from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from archer_processor.core import default_artifact_rules
from archer_processor.services import credentials


@dataclass(slots=True)
class AppSettings:
    history_workbook: str = r"C:\Users\molpa\Desktop\HTS\Resultat_VPM\2026_VPM_Variantfunn.xlsx"
    default_output_dir: str = str(Path.home() / "Desktop")
    clinvar_api_key: str = ""
    cosmic_email: str = ""
    cosmic_password: str = field(default="", repr=False, metadata={"persist": False})
    oncokb_api_key: str = ""
    oncokb_email: str = ""
    oncokb_password: str = field(default="", repr=False, metadata={"persist": False})
    franklin_api_key: str = ""
    franklin_email: str = ""
    franklin_password: str = field(default="", repr=False, metadata={"persist": False})
    mtbp_email: str = ""
    mtbp_password: str = field(default="", repr=False, metadata={"persist": False})
    database_workers: int = 1
    browser_delay_seconds: int = 10
    browser_delay_max_seconds: int = 20
    mtbp_timeout_minutes: int = 20
    search_included_only: bool = True
    gnomad_dataset: str = "gnomad_r2_1"
    mtbp_cancer_type: str = "Blood"
    artifact_rules: list[dict[str, str]] = field(default_factory=default_artifact_rules)
    enabled_databases: list[str] = field(
        default_factory=lambda: [
            "MTBP",
            "Franklin",
            "ClinVar",
            "OncoKB",
            "COSMIC",
        ]
    )

    @classmethod
    def config_path(cls) -> Path:
        return Path.home() / ".archer-prosess" / "config.json"

    @classmethod
    def load(cls) -> "AppSettings":
        path = cls.config_path()
        if not path.exists():
            return cls()
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            persisted_fields = {
                item.name
                for item in fields(cls)
                if item.metadata.get("persist") is not False
            }
            settings = cls(**{key: value for key, value in data.items() if key in persisted_fields})
        except Exception:
            return cls()
        # Migrate former defaults to the new 10-20 second website-only range while
        # preserving any delay range the user actually customized.
        legacy_fixed_delay = (
            "browser_delay_max_seconds" not in data
            and settings.browser_delay_seconds == 15
        )
        former_default_range = (
            (settings.browser_delay_seconds, settings.browser_delay_max_seconds)
            in {(15, 30), (5, 15)}
        )
        if legacy_fixed_delay or former_default_range:
            settings.browser_delay_seconds = 10
            settings.browser_delay_max_seconds = 20
        settings.browser_delay_seconds = max(0, int(settings.browser_delay_seconds))
        settings.browser_delay_max_seconds = max(
            settings.browser_delay_seconds,
            int(settings.browser_delay_max_seconds),
        )
        try:
            settings.oncokb_password = credentials.get_saved_password(
                "OncoKB", settings.oncokb_email
            )
            settings.cosmic_password = credentials.get_saved_password(
                "COSMIC", settings.cosmic_email
            )
            settings.franklin_password = credentials.get_saved_password(
                "Franklin", settings.franklin_email
            )
            settings.mtbp_password = credentials.get_saved_password(
                "MTBP", settings.mtbp_email
            )
        except Exception:
            pass
        return settings

    def save(self) -> None:
        self.browser_delay_seconds = max(0, int(self.browser_delay_seconds))
        self.browser_delay_max_seconds = max(
            self.browser_delay_seconds,
            int(self.browser_delay_max_seconds),
        )
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        for item in fields(self):
            if item.metadata.get("persist") is False:
                data.pop(item.name, None)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        credentials.save_password("OncoKB", self.oncokb_email, self.oncokb_password)
        credentials.save_password("COSMIC", self.cosmic_email, self.cosmic_password)
        credentials.save_password("Franklin", self.franklin_email, self.franklin_password)
        credentials.save_password("MTBP", self.mtbp_email, self.mtbp_password)
