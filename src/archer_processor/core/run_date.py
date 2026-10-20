from __future__ import annotations

import re
from datetime import date
from pathlib import Path


def sequencing_date_from_path(input_path: Path) -> str | None:
    """Read the sequencing date from a VPM file name or enclosing folder."""
    for part in (input_path, *input_path.parents):
        match = re.match(r"^(\d{4})_(\d{2})_(\d{2})_VPM(?=$|[_. -])", part.name, re.I)
        if match:
            try:
                return date(*(int(part) for part in match.groups())).isoformat()
            except ValueError:
                return None
    return None
