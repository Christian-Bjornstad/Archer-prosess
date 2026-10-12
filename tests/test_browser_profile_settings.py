from copy import deepcopy
from pathlib import Path
import time

import pytest

from archer_processor.gui import app
from archer_processor.services.browser_review import BrowserReviewService
from archer_processor.services.browser_sessions import BrowserSessionCheckService, BrowserSessionStatus
from archer_processor.services.settings import AppSettings


@pytest.fixture
def profile_window(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(AppSettings, "load", classmethod(lambda cls: cls(default_output_dir=str(tmp_path))))
    monkeypatch.setattr(AppSettings, "save", lambda self: None)
    monkeypatch.setattr(app.QMessageBox, "warning", lambda *args: None)
    window = app.MainWindow()
    yield window
    window.close()


def test_custom_browser_profile_root_persists_without_creating_profiles(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    profiles = tmp_path / "new-profiles"
    monkeypatch.setattr(AppSettings, "config_path", classmethod(lambda cls: config))
    monkeypatch.setattr("archer_processor.services.settings.credentials.save_password", lambda *args: None)
    monkeypatch.setattr("archer_processor.services.settings.credentials.get_saved_password", lambda *args: "")
    AppSettings(browser_profile_root=str(profiles)).save()
    assert AppSettings.load().browser_profile_root == str(profiles)
    assert not profiles.exists()


@pytest.mark.parametrize("worker_kind", ["login", "session", "database", "browser"])
@pytest.mark.parametrize("custom", [False, True], ids=["legacy-default", "custom-saved-root"])
def test_all_browser_workers_use_the_saved_profile_root(qt_app, tmp_path, monkeypatch, worker_kind, custom):
    custom_root = tmp_path / "operator-selected-root"
    settings = AppSettings(browser_profile_root=str(custom_root) if custom else "")
    expected = custom_root if custom else Path.home() / ".archer-prosess" / "browser_profiles"
    observed = []

    if worker_kind == "login":
        def login(service, database):
            observed.append(service.profile_root)
            return "Synthetic sign-in complete"
        monkeypatch.setattr(BrowserReviewService, "open_login", login)
        app.BrowserLoginWorker("COSMIC", settings).run()
    elif worker_kind == "session":
        def check(checker, database):
            observed.append(checker.review_service.profile_root)
            return BrowserSessionStatus(database, "public", "Synthetic access")
        monkeypatch.setattr(BrowserSessionCheckService, "check", check)
        app.BrowserSessionCheckWorker("ClinVar", settings=settings).run()
    elif worker_kind == "database":
        worker = app.DatabaseWorker([], [], ["ClinVar"], tmp_path, settings)
        observed.append(worker._browser_service().profile_root)
    else:
        worker = app.BrowserReviewWorker([], ["ClinVar"], tmp_path, settings)
        observed.append(worker._build_service().profile_root)

    assert observed == [expected]
    assert not custom_root.exists()


@pytest.mark.parametrize("save_method", ["_save_settings", "_save_browser_settings"])
def test_saved_browser_path_expands_environment_without_creating_directory(profile_window, tmp_path, monkeypatch, save_method):
    root = tmp_path / "local-store"
    monkeypatch.setenv("ARCHER_PROFILE_TEST", str(root))
    profile_window.browser_profile_root_edit.setText("  %ARCHER_PROFILE_TEST%/evidence  ")
    assert getattr(profile_window, save_method)() is True
    expected = str((root / "evidence").resolve())
    assert profile_window.settings.browser_profile_root == expected
    assert profile_window.browser_profile_root_edit.text() == expected
    assert expected in profile_window.browser_profile_status.text()
    assert not root.exists()


def test_local_profile_button_is_an_explicit_unsaved_choice(profile_window, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    profile_window.browser_local_profile_btn.click()
    expected = tmp_path / "local-app-data" / ".archer-prosess" / "browser_profiles"
    assert Path(profile_window.browser_profile_root_edit.text()) == expected
    assert profile_window.settings.browser_profile_root == ""
    assert "Ikke lagret" in profile_window.browser_profile_status.text()
    assert not expected.exists()


def test_local_profile_button_falls_back_to_legacy_root_without_localappdata(profile_window, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    profile_window.browser_local_profile_btn.click()
    assert Path(profile_window.browser_profile_root_edit.text()) == Path.home() / ".archer-prosess" / "browser_profiles"
    assert profile_window.settings.browser_profile_root == ""


def test_browse_profile_folder_keeps_selection_unsaved(profile_window, tmp_path, monkeypatch):
    selected = str(tmp_path / "selected-root")
    monkeypatch.setattr(app.QFileDialog, "getExistingDirectory", lambda *args: selected)
    profile_window.browser_profile_browse_btn.click()
    assert profile_window.browser_profile_root_edit.text() == selected
    assert profile_window.settings.browser_profile_root == ""
    assert not Path(selected).exists()


def wait_for_browser_worker(window, qt_app):
    deadline = time.monotonic() + 3
    while (window._operation_active or window.browser_thread is not None
           or window.session_check_thread is not None) and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not window._operation_active
    assert window.browser_thread is None
    assert window.session_check_thread is None


def test_gui_session_check_uses_saved_root_without_accepting_draft(profile_window, qt_app, tmp_path, monkeypatch):
    saved_root = tmp_path / "saved-root"
    profile_window.settings.browser_profile_root = str(saved_root)
    profile_window.browser_profile_root_edit.setText(str(tmp_path / "unsaved-root"))
    observed = []
    def check(checker, database):
        observed.append(checker.review_service.profile_root)
        return BrowserSessionStatus(database, "public", "Synthetic access")
    monkeypatch.setattr(BrowserSessionCheckService, "check", check)
    monkeypatch.setattr(AppSettings, "save", lambda self: pytest.fail("Session checks must not save settings"))
    profile_window._check_browser_sessions("ClinVar")
    wait_for_browser_worker(profile_window, qt_app)
    assert observed == [saved_root]
    assert profile_window.settings.browser_profile_root == str(saved_root)
    assert not saved_root.exists()


def test_gui_login_uses_selected_root_despite_invalid_reference_drafts(profile_window, qt_app, tmp_path, monkeypatch):
    selected_root = tmp_path / "selected-root"
    profile_window.browser_profile_root_edit.setText(str(selected_root))
    profile_window.who_genes_edit.setText(str(tmp_path / "missing-who.xlsx"))
    observed = []
    def login(service, database):
        observed.append(service.profile_root)
        return "Synthetic sign-in complete"
    monkeypatch.setattr(BrowserReviewService, "open_login", login)
    profile_window._start_browser_login("COSMIC")
    wait_for_browser_worker(profile_window, qt_app)
    assert observed == [selected_root]
    assert profile_window.settings.browser_profile_root == str(selected_root)
    assert profile_window.settings.who_driver_genes_path == ""
    assert not selected_root.exists()


def test_browser_profile_save_ignores_disconnected_reference_paths(profile_window, tmp_path, monkeypatch):
    original_output = str(tmp_path / "offline-report-share")
    original_who = str(tmp_path / "offline-who.xlsx")
    profile_window.settings.default_output_dir = original_output
    profile_window.settings.who_driver_genes_path = original_who
    profile_window.output_dir_edit.setText(str(tmp_path / "unsaved-report-share"))
    profile_window.who_genes_edit.setText(str(tmp_path / "unsaved-who.xlsx"))
    profile_window.browser_profile_root_edit.setText(str(tmp_path / "profiles"))
    assert profile_window._save_browser_settings() is True
    assert profile_window.settings.browser_profile_root == str(tmp_path / "profiles")
    assert profile_window.settings.default_output_dir == original_output
    assert profile_window.settings.who_driver_genes_path == original_who


@pytest.mark.parametrize("save_method", ["_save_settings", "_save_browser_settings"])
def test_existing_file_cannot_be_selected_as_profile_folder(profile_window, tmp_path, save_method):
    existing = tmp_path / "keep.txt"
    existing.write_text("Keep existing contents", encoding="utf-8")
    profile_window.browser_profile_root_edit.setText(str(existing))
    previous = profile_window.settings
    assert getattr(profile_window, save_method)() is False
    assert profile_window.settings is previous
    assert profile_window.settings.browser_profile_root == ""
    assert existing.read_text(encoding="utf-8") == "Keep existing contents"


@pytest.mark.parametrize("save_method", ["_save_settings", "_save_browser_settings"])
@pytest.mark.parametrize("save_error", [OSError, ValueError])
def test_failed_profile_save_keeps_active_root(profile_window, tmp_path, monkeypatch, save_method, save_error):
    previous = profile_window.settings
    profile_window.browser_profile_root_edit.setText(str(tmp_path / "new-root"))
    def fail(candidate):
        raise save_error("Synthetic save failure")
    monkeypatch.setattr(AppSettings, "save", fail)
    assert getattr(profile_window, save_method)() is False
    assert profile_window.settings is previous
    assert profile_window.settings.browser_profile_root == ""
    assert "Ikke lagret" in profile_window.browser_profile_status.text()


def test_changing_saved_profile_root_invalidates_old_session_checks(profile_window, tmp_path):
    profile_window._session_checked(BrowserSessionStatus("COSMIC", "authenticated", "Prior profile"))
    profile_window._session_checked(BrowserSessionStatus("ClinVar", "public", "Prior profile"))
    evidence = deepcopy(profile_window.evidence)
    profile_window.browser_profile_root_edit.setText(str(tmp_path / "new-root"))
    assert profile_window._save_browser_settings() is True
    assert profile_window._session_results == {}
    assert profile_window.session_status_labels["COSMIC"].text() == "Sjekk på nytt"
    assert profile_window.evidence == evidence


def test_invalid_profile_configuration_does_not_interrupt_recent_workbook_callback(profile_window, tmp_path, monkeypatch):
    profile_window.settings.browser_profile_root = "relative-profile-root"
    path = tmp_path / "review.xlsx"
    def save(candidate):
        raise ValueError("Synthetic invalid profile configuration")
    monkeypatch.setattr(AppSettings, "save", save)
    profile_window._remember_recent_workbook(path)
    assert profile_window.settings.last_processed_workbook == str(path)
    assert profile_window.settings.browser_profile_root == "relative-profile-root"
    assert "Synthetic invalid profile configuration" in profile_window.log.toPlainText()


def test_invalid_persisted_profile_root_is_visible_and_rejected_before_browser(qt_app, tmp_path, monkeypatch):
    settings = AppSettings(default_output_dir=str(tmp_path), browser_profile_root="relative-profile-root")
    monkeypatch.setattr(AppSettings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(AppSettings, "save", lambda self: None)
    window = app.MainWindow()
    try:
        assert window.browser_profile_root_edit.text() == "relative-profile-root"
        assert "Ugyldig" in window.browser_profile_status.text()
        with pytest.raises(ValueError, match="absolutt"):
            app.DatabaseWorker([], [], ["ClinVar"], tmp_path, settings)._browser_service()
        assert not (tmp_path / "relative-profile-root").exists()
    finally:
        window.close()


def test_browse_can_replace_an_invalid_saved_root_after_clearing_draft(profile_window, tmp_path, monkeypatch):
    profile_window.settings.browser_profile_root = "relative-profile-root"
    profile_window.browser_profile_root_edit.clear()
    chosen = str(tmp_path / "valid-profile-root")
    monkeypatch.setattr(app.QFileDialog, "getExistingDirectory", lambda *args: chosen)
    profile_window._browse_browser_profile_root()
    assert profile_window.browser_profile_root_edit.text() == chosen
    assert profile_window.settings.browser_profile_root == "relative-profile-root"
