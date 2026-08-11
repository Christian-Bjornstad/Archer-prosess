from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageStat


@dataclass(frozen=True, slots=True)
class CaptureValidation:
    valid: bool
    reason: str
    width: int
    height: int
    luminance_stddev: float


class IncompleteCaptureError(RuntimeError):
    def __init__(self, validation: CaptureValidation) -> None:
        super().__init__(f"Screenshot validation failed: {validation.reason}")
        self.validation = validation


def validate_capture(
    path: Path,
    *,
    min_width: int = 200,
    min_height: int = 80,
    min_stddev: float = 1.0,
) -> CaptureValidation:
    try:
        with Image.open(path) as image:
            gray = image.convert("L")
            width, height = gray.size
            deviation = float(ImageStat.Stat(gray).stddev[0])
    except (OSError, ValueError):
        return CaptureValidation(False, "unreadable", 0, 0, 0.0)
    if width < min_width or height < min_height:
        return CaptureValidation(False, "too_small", width, height, deviation)
    if deviation < min_stddev:
        return CaptureValidation(False, "low_content", width, height, deviation)
    return CaptureValidation(True, "ok", width, height, deviation)
