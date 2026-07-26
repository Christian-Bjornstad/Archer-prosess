from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AppSettings:
    history_workbook: str = r"C:\Users\molpa\Desktop\HTS\Resultat_VPM\2026_VPM_Variantfunn.xlsx"
    default_output_dir: str = str(Path.home() / "Desktop")
    clinvar_api_key: str = ""
    oncokb_api_key: str = ""
    database_workers: int = 3
    gnomad_dataset: str = "gnomad_r2_1"
    enabled_databases: list[str] = field(default_factory=lambda: ["ClinVar"])

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
            return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__})
        except Exception:
            return cls()

    def save(self) -> None:
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
