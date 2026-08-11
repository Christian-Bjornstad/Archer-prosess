from __future__ import annotations

from collections.abc import Sequence
from typing import Any


OVERLAY_SELECTOR = (
    "[role='dialog'], [aria-modal='true'], #onetrust-banner-sdk, "
    "#onetrust-consent-sdk, [class*='cookie-consent'], "
    "[class*='consent-banner'], [class*='modal'], [class*='overlay']"
)

BUTTON_LABELS = (
    "Reject all",
    "Accept essential",
    "Accept necessary",
    "Only necessary",
    "No thanks",
    "Maybe later",
    "Got it",
)


def choose_overlay_action(labels: Sequence[str]) -> str:
    normalized = {label.strip().casefold(): label for label in labels}
    for preferred in BUTTON_LABELS:
        match = normalized.get(preferred.casefold())
        if match:
            return match
    return ""


def dismiss_known_overlays(page: Any) -> list[str]:
    clicked: list[str] = []
    try:
        containers = page.locator(OVERLAY_SELECTOR)
        for index in range(containers.count()):
            container = containers.nth(index)
            if not container.is_visible():
                continue
            buttons = container.locator("button")
            labels = buttons.all_inner_texts()
            action = choose_overlay_action(labels)
            if action:
                for button_index, label in enumerate(labels):
                    if label.strip() != action:
                        continue
                    button = buttons.nth(button_index)
                    if button.is_visible():
                        button.click()
                        clicked.append(action)
                    break
    except Exception:
        return clicked
    if clicked:
        page.wait_for_timeout(350)
    return clicked
