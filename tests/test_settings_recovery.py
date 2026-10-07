import json

import pytest

from archer_processor.core import default_artifact_rules
from archer_processor.services.settings import AppSettings


@pytest.mark.parametrize("bad_field,value", [
    ("artifact_catalog_version", "v4"),
    ("browser_delay_seconds", "not-a-number"),
    ("browser_delay_max_seconds", None),
    ("artifact_rules", None),
    ("artifact_rules", [None]),
    ("artifact_rules", [{"hgvsc": "NM_000546.6:c.524G>A", "max_af": "bad"}]),
    ("enabled_databases", "ClinVar"),
    ("browser_background", "false"),
    ("who_driver_genes_path", 42),
])
def test_invalid_config_field_recovers_without_losing_other_fields(tmp_path, monkeypatch, bad_field, value):
    config = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config))
    monkeypatch.setattr("archer_processor.services.settings.credentials.get_saved_password", lambda *args: "")
    payload = {"artifact_catalog_version": 4, "default_output_dir": str(tmp_path),
               "mtbp_email": "synthetic@example.org", bad_field: value}
    config.write_text(json.dumps(payload), encoding="utf-8")
    loaded = AppSettings.load()
    assert loaded.default_output_dir == str(tmp_path)
    assert loaded.mtbp_email == "synthetic@example.org"
    assert getattr(loaded, bad_field) == getattr(AppSettings(), bad_field)
    assert any(bad_field in message for message in loaded.load_warnings)


def test_valid_custom_rules_and_zero_delays_are_preserved(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config))
    monkeypatch.setattr("archer_processor.services.settings.credentials.get_saved_password", lambda *args: "")
    custom = [{"gene": "TP53", "hgvsc": "NM_000546.6:c.524G>A", "max_af": "0%", "reason": "Synthetic"}]
    config.write_text(json.dumps({"artifact_catalog_version": 4, "artifact_rules": custom,
                                  "browser_delay_seconds": 0, "browser_delay_max_seconds": 0}), encoding="utf-8")
    loaded = AppSettings.load()
    assert loaded.artifact_rules == custom
    assert loaded.browser_delay_seconds == loaded.browser_delay_max_seconds == 0
    assert loaded.load_warnings == []


def test_malformed_config_warning_is_visible_and_not_persisted(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config))
    monkeypatch.setattr("archer_processor.services.settings.credentials.save_password", lambda *args: None)
    config.write_text("{broken", encoding="utf-8")
    loaded = AppSettings.load()
    assert loaded.artifact_rules == default_artifact_rules()
    assert loaded.load_warnings
    loaded.save()
    assert "load_warnings" not in json.loads(config.read_text(encoding="utf-8"))
