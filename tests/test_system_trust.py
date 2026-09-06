import ssl
from types import SimpleNamespace

import pytest
import requests

from archer_processor.services.system_trust import SystemTrustAdapter
from archer_processor.services.database_search import DatabaseSearchService
from archer_processor.services.settings import AppSettings


def test_system_adapter_preserves_certificate_and_hostname_verification():
    adapter = SystemTrustAdapter()
    request = requests.Request('GET', 'https://eutils.ncbi.nlm.nih.gov/').prepare()
    _, attributes = adapter.build_connection_pool_key_attributes(request, True)
    context = attributes['ssl_context']
    assert context is adapter.system_context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_clinvar_reuses_os_trust_after_certificate_failure(monkeypatch):
    service = DatabaseSearchService(AppSettings())
    monkeypatch.setattr(service, '_wait_for_eutils_slot', lambda: None)
    calls = []
    response = SimpleNamespace(status_code=200)

    def default_get(*args, **kwargs):
        calls.append('default')
        raise requests.exceptions.SSLError('self-signed certificate in certificate chain')

    def system_get(*args, **kwargs):
        assert kwargs.get('verify', True) is True
        calls.append('system')
        return response

    monkeypatch.setattr(requests, 'get', default_get)
    monkeypatch.setattr('archer_processor.services.database_search.system_trust_session', lambda: SimpleNamespace(get=system_get))
    for _ in range(2):
        assert service._eutils_get('https://eutils.ncbi.nlm.nih.gov/', {}) is response
    assert calls == ['default', 'system', 'system']


def test_untrusted_certificate_still_fails(monkeypatch):
    service = DatabaseSearchService(AppSettings())
    monkeypatch.setattr(service, '_wait_for_eutils_slot', lambda: None)
    def fail(*args, **kwargs):
        raise requests.exceptions.SSLError('untrusted')
    monkeypatch.setattr(requests, 'get', fail)
    monkeypatch.setattr('archer_processor.services.database_search.system_trust_session', lambda: SimpleNamespace(get=fail))
    with pytest.raises(requests.exceptions.SSLError):
        service._eutils_get('https://eutils.ncbi.nlm.nih.gov/', {})
