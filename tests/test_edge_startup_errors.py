from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from archer_processor.services.browser_review import (
    BROWSER_DATABASES, BrowserAutomationUnavailable, BrowserReviewService,
)
from archer_processor.services.browser_sessions import BrowserSessionCheckService
from archer_processor.services.edge_cdp import (
    EdgeCdpError, EdgeCdpPolicyError, EdgeCdpProfileInUse, EdgeCdpTimeout,
)


def failing_runtime(monkeypatch, service, error):
    def launch(*args, **kwargs):
        raise error

    @contextmanager
    def runtime():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    monkeypatch.setattr(service, '_browser_api', lambda: (runtime, EdgeCdpError, EdgeCdpTimeout))


@pytest.mark.parametrize('database', BROWSER_DATABASES)
@pytest.mark.parametrize('error', [
    EdgeCdpPolicyError('UserDataDir overrides the isolated profile'),
    EdgeCdpProfileInUse('Dedicated profile is already in use'),
    EdgeCdpTimeout('Local endpoint never opened; Startup log: demo.log'),
    PermissionError(13, 'Profile directory access denied'),
])
def test_login_preserves_actual_startup_reason_and_profile(tmp_path, monkeypatch, database, error):
    service = BrowserReviewService(profile_root=tmp_path / 'profiles')
    failing_runtime(monkeypatch, service, error)
    with pytest.raises(BrowserAutomationUnavailable) as raised:
        service.open_login(database)
    message = str(raised.value)
    assert str(error) in message
    assert type(error).__name__ in message
    assert str(service.profile_directory(database)) in message
    assert 'policy is enabled' not in message
    assert raised.value.__cause__ is error


def test_profile_creation_failure_is_an_actionable_startup_error(tmp_path, monkeypatch):
    root = tmp_path / 'not-a-directory'
    root.touch()
    service = BrowserReviewService(profile_root=root)
    failing_runtime(monkeypatch, service, RuntimeError('must not reach launch'))
    with pytest.raises(BrowserAutomationUnavailable) as raised:
        service.open_login('OncoKB')
    assert str(root) in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)


def test_session_check_preserves_profile_access_failure(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path / 'profiles')
    error = PermissionError(13, 'Profile directory access denied')
    failing_runtime(monkeypatch, service, error)
    result = BrowserSessionCheckService(service).check('OncoKB')
    assert result.status == 'error'
    assert str(error) in result.message
    assert str(service.profile_directory('OncoKB')) in result.message


def test_unknown_startup_exception_does_not_expose_arbitrary_contents(tmp_path, monkeypatch):
    service = BrowserReviewService(profile_root=tmp_path / 'profiles')
    failing_runtime(monkeypatch, service, RuntimeError('secret-token@example.invalid'))
    with pytest.raises(BrowserAutomationUnavailable) as raised:
        service.open_login('OncoKB')
    assert 'RuntimeError' in str(raised.value)
    assert 'secret-token' not in str(raised.value)
