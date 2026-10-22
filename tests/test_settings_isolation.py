import json
from pathlib import Path

import keyring
import pytest

from archer_processor.gui.app import MainWindow
from archer_processor.services.settings import AppSettings


@pytest.mark.parametrize("password", ["synthetic-first", "synthetic-second"])
def test_gui_load_and_save_leave_workstation_settings_and_credentials_untouched(
    qt_app, tmp_path, monkeypatch, password
):
    config = AppSettings.config_path()
    assert config.is_relative_to(tmp_path)

    personal_home = tmp_path / "workstation-profile"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: personal_home))

    def real_credential_backend():
        raise AssertionError("Tests must not access the workstation credential backend")

    monkeypatch.setattr(keyring, "get_keyring", real_credential_backend)
    email = "synthetic@example.invalid"
    service = "Archer Prosess/COSMIC"
    assert keyring.get_password(service, email) is None
    AppSettings(
        default_output_dir=str(tmp_path),
        cosmic_email=email,
        cosmic_password=password,
    ).save()

    window = MainWindow()
    try:
        assert window.cosmic_email_edit.text() == email
        assert window.cosmic_password_edit.text() == password
        window.cosmic_password_edit.setText("synthetic-updated")

        assert window._save_settings(silent=True)
        assert AppSettings.load().cosmic_password == "synthetic-updated"
        assert keyring.get_password(service, email) == "synthetic-updated"
        stored = json.loads(config.read_text(encoding="utf-8"))
        assert stored["cosmic_email"] == email
        assert "cosmic_password" not in stored
        assert not (personal_home / ".archer-prosess").exists()
    finally:
        window.close()
