"""Requests adapter using OS roots, including managed Windows enterprise CAs."""
import ssl

import requests
from requests.adapters import HTTPAdapter


class SystemTrustAdapter(HTTPAdapter):
    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        if verify is True:
            kwargs["ssl_context"] = self.system_context
        return host, kwargs

    def init_poolmanager(self, *args, **kwargs):
        self.system_context = ssl.create_default_context()
        kwargs["ssl_context"] = self.system_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        kwargs["ssl_context"] = self.system_context
        kwargs["proxy_ssl_context"] = self.system_context
        return super().proxy_manager_for(proxy, **kwargs)


def system_trust_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", SystemTrustAdapter())
    return session
