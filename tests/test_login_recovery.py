from copy import deepcopy
import time
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from archer_processor.gui import app
from archer_processor.services.browser_sessions import BrowserSessionStatus
from archer_processor.services.credentials import CredentialStoreError
from archer_processor.services.settings import AppSettings


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    monkeypatch.setattr(AppSettings, "load", classmethod(lambda cls: cls(default_output_dir=str(tmp_path))))
    monkeypatch.setattr(AppSettings, "save", lambda self: None)
    value = app.MainWindow()
    yield value
    value.close()


def wait_for_recovery(window, qt_app):
    deadline = time.monotonic() + 3
    while (window._operation_active or window.browser_thread is not None
           or window.session_check_thread is not None) and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert not window._operation_active
    assert window.browser_thread is None
    assert window.session_check_thread is None


def test_login_saves_browser_options_despite_disconnected_and_unsaved_reference_paths(window, qt_app, monkeypatch, tmp_path):
    saved, logins, warnings = [], [], []
    monkeypatch.setattr(AppSettings, "save", lambda self: saved.append(deepcopy(self)))
    monkeypatch.setattr(app.QMessageBox, "warning", lambda *args: warnings.append(args))

    class Login(QObject):
        finished = pyqtSignal(str)
        failed = pyqtSignal(str)
        status = pyqtSignal(str)

        def __init__(self, database, settings):
            super().__init__()
            self.database = database
            logins.append((database, deepcopy(settings)))

        def run(self):
            self.finished.emit("Synthetic sign-in finished")

    monkeypatch.setattr(app, "BrowserLoginWorker", Login)
    window.settings.default_output_dir = str(tmp_path / "disconnected-report-share")
    window.settings.who_driver_genes_path = str(tmp_path / "disconnected-who.xlsx")
    window.settings.artifact_rules_path = str(tmp_path / "disconnected-artifacts.xlsx")
    previous_rules = deepcopy(window.settings.artifact_rules)
    window.output_dir_edit.setText(str(tmp_path / "unsaved-output"))
    window.who_genes_edit.setText(str(tmp_path / "unsaved-who.xlsx"))
    window.artifact_path_edit.setText(str(tmp_path / "unsaved-artifacts.xlsx"))
    window.cosmic_email_edit.setText("synthetic@example.invalid")
    window.cosmic_password_edit.setText("synthetic-password")
    window.browser_delay_spin.setValue(4)
    window.browser_delay_max_spin.setValue(2)
    window.browser_background_check.setChecked(False)
    window.mtbp_timeout_spin.setValue(9)
    window.mtbp_cancer_type_edit.setText("Synthetic type")
    window.browser_database_combo.setCurrentText("COSMIC")

    window._start_browser_login()
    wait_for_recovery(window, qt_app)

    assert not warnings
    assert len(logins) == len(saved) == 1
    settings = saved[0]
    assert logins[0][0] == "COSMIC"
    assert settings.cosmic_email == "synthetic@example.invalid"
    assert settings.cosmic_password == "synthetic-password"
    assert (settings.browser_delay_seconds, settings.browser_delay_max_seconds) == (4, 4)
    assert settings.browser_background is False
    assert settings.mtbp_timeout_minutes == 9
    assert settings.mtbp_cancer_type == "Synthetic type"
    assert settings.default_output_dir == str(tmp_path / "disconnected-report-share")
    assert settings.who_driver_genes_path == str(tmp_path / "disconnected-who.xlsx")
    assert settings.artifact_rules_path == str(tmp_path / "disconnected-artifacts.xlsx")
    assert settings.artifact_rules == previous_rules
    assert window.who_genes_edit.text() == str(tmp_path / "unsaved-who.xlsx")


@pytest.mark.parametrize("error", [OSError("Synthetic settings failure"), CredentialStoreError("Synthetic credentials failure")])
def test_browser_setting_save_failure_keeps_previous_settings_and_does_not_start_login(window, monkeypatch, error):
    original = window.settings
    warnings = []
    window.cosmic_email_edit.setText("unsaved@example.invalid")
    monkeypatch.setattr(app.QMessageBox, "warning", lambda *args: warnings.append(args))

    def fail_save(self):
        raise error

    monkeypatch.setattr(AppSettings, "save", fail_save)
    monkeypatch.setattr(app, "BrowserLoginWorker", lambda *args: pytest.fail("Cannot start after save failure"))

    window._start_browser_login("COSMIC")

    assert window.settings is original
    assert window.settings.cosmic_email == ""
    assert window.browser_thread is None
    assert not window._operation_active
    assert str(error) in warnings[0][2]


def test_single_provider_recheck_preserves_other_results_and_does_not_save_settings(window, qt_app, monkeypatch):
    from archer_processor.services import browser_sessions

    prior = BrowserSessionStatus("ClinVar", "public", "Synthetic prior access")
    window._session_checked(prior)
    checked = []

    class Checker:
        def __init__(self, review):
            pass

        def check(self, database):
            checked.append(database)
            return BrowserSessionStatus(database, "login_required", "Synthetic login needed")

        def check_all(self, **kwargs):
            pytest.fail("A single-provider retry must not start every profile")

    monkeypatch.setattr(browser_sessions, "BrowserSessionCheckService", Checker)
    monkeypatch.setattr(AppSettings, "save", lambda self: pytest.fail("Checking access must not persist settings"))

    window._check_browser_sessions("Franklin")
    wait_for_recovery(window, qt_app)

    assert checked == ["Franklin"]
    assert window._session_results["ClinVar"] is prior
    assert window.session_status_labels["ClinVar"].text() == "Offentlig tilgang"
    assert window.session_status_labels["Franklin"].text() == "Krever innlogging"
    assert "Franklin" in window.session_check_summary.text()


def test_single_provider_worker_failure_leaves_untargeted_statuses_intact(window, qt_app, monkeypatch):
    from archer_processor.services import browser_sessions

    window._session_checked(BrowserSessionStatus("ClinVar", "public", "Synthetic prior access"))
    cosmic_label = window.session_status_labels["COSMIC"].text()

    class Checker:
        def __init__(self, review):
            pass

        def check(self, database):
            raise RuntimeError("Synthetic worker failure")

    monkeypatch.setattr(browser_sessions, "BrowserSessionCheckService", Checker)
    window._check_browser_sessions("Franklin")
    wait_for_recovery(window, qt_app)

    assert window.session_status_labels["Franklin"].text() == "Kunne ikke sjekke"
    assert window.session_status_labels["COSMIC"].text() == cosmic_label
    assert window.session_status_labels["ClinVar"].text() == "Offentlig tilgang"


@pytest.mark.parametrize("method", ["_start_browser_login", "_check_browser_sessions"])
@pytest.mark.parametrize("thread_name", ["database_thread", "browser_thread", "session_check_thread", "workbook_write_thread"])
def test_recovery_cannot_launch_while_an_existing_worker_is_still_running(window, monkeypatch, method, thread_name):
    monkeypatch.setattr(window, thread_name, SimpleNamespace(isRunning=lambda: True))
    monkeypatch.setattr(AppSettings, "save", lambda self: pytest.fail("Do not save while a worker owns the operation"))
    monkeypatch.setattr(app, "BrowserLoginWorker", lambda *args: pytest.fail("Do not start a second login"))
    monkeypatch.setattr(app, "BrowserSessionCheckWorker", lambda *args: pytest.fail("Do not start a second check"))

    getattr(window, method)("Franklin")

    assert not window._operation_active
    setattr(window, thread_name, None)


def test_session_terminal_result_keeps_busy_until_its_thread_exits(window):
    window._set_busy("Checking browser sessions")
    window.session_check_thread = SimpleNamespace(isRunning=lambda: True)
    window._session_check_finished([BrowserSessionStatus("ClinVar", "public", "Synthetic access")])
    assert window._operation_active
    assert not window.check_sessions_btn.isEnabled()
    window.session_check_thread = None
    window._set_ready()


def test_login_completion_invalidates_the_actual_provider_after_combo_changes(window, qt_app, monkeypatch):
    window._session_checked(BrowserSessionStatus("COSMIC", "authenticated", "Synthetic prior access"))
    prior = BrowserSessionStatus("ClinVar", "public", "Synthetic prior access")
    window._session_checked(prior)

    class Login(QObject):
        finished = pyqtSignal(str)
        failed = pyqtSignal(str)
        status = pyqtSignal(str)

        def __init__(self, database, settings):
            super().__init__()
            self.database = database

        def run(self):
            self.finished.emit("Synthetic login finished")

    monkeypatch.setattr(app, "BrowserLoginWorker", Login)
    window._start_browser_login("COSMIC")
    window.browser_database_combo.setCurrentText("ClinVar")
    wait_for_recovery(window, qt_app)

    assert "COSMIC" not in window._session_results
    assert window.session_status_labels["COSMIC"].text() == "Sjekk på nytt"
    assert window._session_results["ClinVar"] is prior
    assert window.session_status_labels["ClinVar"].text() == "Offentlig tilgang"


def test_login_failure_invalidates_only_its_provider_and_releases_worker(window, qt_app, monkeypatch):
    previous = BrowserSessionStatus("ClinVar", "public", "Synthetic prior access")
    window._session_checked(previous)
    window._session_checked(BrowserSessionStatus("COSMIC", "authenticated", "Synthetic prior access"))
    errors = []
    monkeypatch.setattr(app.QMessageBox, "critical", lambda *args: errors.append(args))

    class Login(QObject):
        finished = pyqtSignal(str)
        failed = pyqtSignal(str)
        status = pyqtSignal(str)

        def __init__(self, database, settings):
            super().__init__()
            self.database = database

        def run(self):
            self.failed.emit("Synthetic policy block")

    monkeypatch.setattr(app, "BrowserLoginWorker", Login)
    window._start_browser_login("COSMIC")
    wait_for_recovery(window, qt_app)

    assert "COSMIC" not in window._session_results
    assert window._session_results["ClinVar"] is previous
    assert window.session_status_labels["COSMIC"].text() == "Sjekk på nytt"
    assert errors[0][2] == "Synthetic policy block"


def test_existing_signin_button_uses_selected_provider_with_qt_checked_argument(window, qt_app, monkeypatch):
    logins = []

    class Login(QObject):
        finished = pyqtSignal(str)
        failed = pyqtSignal(str)
        status = pyqtSignal(str)

        def __init__(self, database, settings):
            super().__init__()
            self.database = database
            logins.append(database)

        def run(self):
            self.finished.emit("Synthetic sign-in completed")

    monkeypatch.setattr(app, "BrowserLoginWorker", Login)
    window.browser_database_combo.setCurrentText("Franklin")
    window.browser_signin_btn.click()
    wait_for_recovery(window, qt_app)
    assert logins == ["Franklin"]


@pytest.mark.parametrize("method", ["_start_browser_login", "_check_browser_sessions"])
def test_invalid_gui_provider_is_rejected_before_settings_or_browser_side_effects(window, monkeypatch, method):
    monkeypatch.setattr(AppSettings, "save", lambda self: pytest.fail("Unknown provider must not save settings"))
    with pytest.raises(ValueError, match="Unknown evidence provider"):
        getattr(window, method)("Unexpected")
    assert window.browser_thread is None
    assert window.session_check_thread is None
    assert not window._operation_active
