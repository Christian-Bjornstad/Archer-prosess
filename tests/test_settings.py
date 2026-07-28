import json

from archer_processor.services import AppSettings


def test_franklin_password_is_runtime_only_and_not_saved(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config_path))

    settings = AppSettings(franklin_email="user@example.org", franklin_password="secret")
    settings.save()

    data = json.loads(config_path.read_text(encoding="utf-8"))
    loaded = AppSettings.load()

    assert data["franklin_email"] == "user@example.org"
    assert "franklin_password" not in data
    assert loaded.franklin_email == "user@example.org"
    assert loaded.franklin_password == ""


def test_franklin_password_is_ignored_if_present_in_old_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config_path))
    config_path.write_text(
        json.dumps({"franklin_email": "user@example.org", "franklin_password": "old-secret"}),
        encoding="utf-8",
    )

    loaded = AppSettings.load()

    assert loaded.franklin_email == "user@example.org"
    assert loaded.franklin_password == ""
