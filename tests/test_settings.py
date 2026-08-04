import json

from archer_processor.services import AppSettings


def test_login_passwords_use_credential_store_not_json(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config_path))
    saved = {}
    monkeypatch.setattr(
        "archer_processor.services.settings.credentials.save_password",
        lambda provider, username, password: saved.__setitem__((provider, username), password),
    )
    monkeypatch.setattr(
        "archer_processor.services.settings.credentials.get_saved_password",
        lambda provider, username: saved.get((provider, username), ""),
    )

    settings = AppSettings(
        cosmic_email="cosmic@example.org",
        cosmic_password="cosmic-secret",
        oncokb_email="oncokb@example.org",
        oncokb_password="oncokb-secret",
        franklin_email="franklin@example.org",
        franklin_password="franklin-secret",
        mtbp_email="mtbp@example.org",
        mtbp_password="mtbp-secret",
    )
    settings.save()

    data = json.loads(config_path.read_text(encoding="utf-8"))
    loaded = AppSettings.load()

    assert data["oncokb_email"] == "oncokb@example.org"
    assert data["cosmic_email"] == "cosmic@example.org"
    assert data["franklin_email"] == "franklin@example.org"
    assert data["mtbp_email"] == "mtbp@example.org"
    assert "oncokb_password" not in data
    assert "cosmic_password" not in data
    assert "franklin_password" not in data
    assert "mtbp_password" not in data
    assert loaded.oncokb_password == "oncokb-secret"
    assert loaded.cosmic_password == "cosmic-secret"
    assert loaded.franklin_password == "franklin-secret"
    assert loaded.mtbp_password == "mtbp-secret"


def test_franklin_password_is_ignored_if_present_in_old_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config_path))
    monkeypatch.setattr(
        "archer_processor.services.settings.credentials.get_saved_password",
        lambda provider, username: "",
    )
    config_path.write_text(
        json.dumps({"franklin_email": "user@example.org", "franklin_password": "old-secret"}),
        encoding="utf-8",
    )

    loaded = AppSettings.load()

    assert loaded.franklin_email == "user@example.org"
    assert loaded.franklin_password == ""


def test_browser_delay_range_normalizes_maximum_to_minimum(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config_path))
    monkeypatch.setattr(
        "archer_processor.services.settings.credentials.get_saved_password",
        lambda provider, username: "",
    )
    config_path.write_text(
        json.dumps(
            {
                "browser_delay_seconds": 25,
                "browser_delay_max_seconds": 20,
            }
        ),
        encoding="utf-8",
    )

    loaded = AppSettings.load()

    assert loaded.browser_delay_seconds == 25
    assert loaded.browser_delay_max_seconds == 25


def test_legacy_fixed_browser_delay_migrates_to_new_range(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config_path))
    monkeypatch.setattr(
        "archer_processor.services.settings.credentials.get_saved_password",
        lambda provider, username: "",
    )
    config_path.write_text(
        json.dumps({"browser_delay_seconds": 15}),
        encoding="utf-8",
    )

    loaded = AppSettings.load()

    assert loaded.browser_delay_seconds == 10
    assert loaded.browser_delay_max_seconds == 20


def test_former_default_browser_delay_range_migrates(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config_path))
    monkeypatch.setattr(
        "archer_processor.services.settings.credentials.get_saved_password",
        lambda provider, username: "",
    )
    config_path.write_text(
        json.dumps(
            {
                "browser_delay_seconds": 15,
                "browser_delay_max_seconds": 30,
            }
        ),
        encoding="utf-8",
    )

    loaded = AppSettings.load()

    assert loaded.browser_delay_seconds == 10
    assert loaded.browser_delay_max_seconds == 20


def test_recent_default_browser_delay_range_migrates(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config_path))
    monkeypatch.setattr(
        "archer_processor.services.settings.credentials.get_saved_password",
        lambda provider, username: "",
    )
    config_path.write_text(
        json.dumps(
            {
                "browser_delay_seconds": 5,
                "browser_delay_max_seconds": 15,
            }
        ),
        encoding="utf-8",
    )

    loaded = AppSettings.load()

    assert loaded.browser_delay_seconds == 10
    assert loaded.browser_delay_max_seconds == 20
