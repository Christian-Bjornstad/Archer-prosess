from pathlib import Path

from PIL import Image, ImageDraw

from archer_processor.services.browser_popups import choose_overlay_action
from archer_processor.services.capture_validation import validate_capture


def test_blank_capture_is_rejected(tmp_path):
    path = tmp_path / "blank.png"
    Image.new("RGB", (800, 500), "white").save(path)

    validation = validate_capture(path)

    assert not validation.valid
    assert validation.reason == "low_content"


def test_content_capture_is_accepted(tmp_path):
    path = tmp_path / "content.png"
    image = Image.new("RGB", (800, 500), "white")
    ImageDraw.Draw(image).rectangle((20, 20, 780, 300), fill="navy")
    image.save(path)

    assert validate_capture(path).valid


def test_popup_action_prefers_rejection_and_ignores_generic_close():
    assert choose_overlay_action(["Close", "Reject all"]) == "Reject all"
    assert (
        choose_overlay_action(["Accept all", "Accept essential"])
        == "Accept essential"
    )
    assert choose_overlay_action(["Close"]) == ""
