"""Verified AI transport using public and operating-system trust roots."""

import os
import ssl

import certifi


def ai_ssl_context() -> ssl.SSLContext:
    # An explicitly configured bundle is authoritative; a bad path must fail.
    for name in ('FPULSE_AI_CA_BUNDLE', 'REQUESTS_CA_BUNDLE', 'SSL_CERT_FILE'):
        path = os.environ.get(name, '').strip()
        if path:
            return ssl.create_default_context(cafile=path)
    context = ssl.create_default_context(cafile=certifi.where())
    # On Windows this includes the ROOT/CA stores used by managed installations.
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return context
