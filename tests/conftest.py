import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import keyring
from PyQt6.QtWidgets import QApplication


@pytest.fixture(autouse=True)
def isolated_workstation_settings(tmp_path, monkeypatch):
    """Keep real settings and OS credentials outside every test's boundary."""
    from archer_processor.services.settings import AppSettings

    config = tmp_path / "settings" / "config.json"
    passwords = {}
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config))
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, username: passwords.get((service, username)),
    )

    def set_password(service, username, password):
        passwords[(service, username)] = password

    monkeypatch.setattr(keyring, "set_password", set_password)


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    return app
