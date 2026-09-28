from __future__ import annotations

import re
from datetime import date
from pathlib import Path


def sequencing_date_from_path(input_path: Path) -> str | None:
    """Read the sequencing date from the nearest YYYY_MM_DD_VPM folder."""
    for parent in input_path.parents:
        match = re.fullmatch(r"(\d{4})_(\d{2})_(\d{2})_VPM", parent.name, re.I)
        if match:
            try:
                return date(*(int(part) for part in match.groups())).isoformat()
            except ValueError:
                return None
    return None
