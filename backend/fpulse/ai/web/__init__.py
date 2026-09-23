"""Opt-in web access for the Copilot (READ-tier tools).

F-Pulse OSS is local-first and ships with **no** web access for the agent —
the Copilot cannot browse the internet by default. Web access is turned on in
one of two ways (either is sufficient):

  * **Runtime toggle** (preferred, no restart): an admin flips "Allow web
    access" in Settings -> AI, which sets ``ai_web_access`` in admin_settings.
    Because the agent re-registers tools per request, the change is live.
  * **Env var** (headless / air-gapped bootstrap): ``FPULSE_AI_WEB_ACCESS=1``.

When enabled, two READ-tier tools are registered:

  * ``web_fetch`` fetches a PUBLIC URL (SSRF-hardened; private/loopback/
    metadata hosts blocked unless ``FPULSE_AI_WEB_ALLOW_PRIVATE=1``). Needs no
    key.
  * ``web_search`` queries a search provider. Provider + key come from the
    admin setting first, then the env vars:

        FPULSE_WEB_SEARCH_PROVIDER=brave|tavily
        FPULSE_WEB_SEARCH_API_KEY=<key>
        FPULSE_WEB_SEARCH_ENDPOINT=<optional override>

    With no provider configured, web_search returns a clear "not configured"
    message rather than pretending to search — no false promises.
"""

from __future__ import annotations

import json
import os

WEB_ACCESS_ENV = "FPULSE_AI_WEB_ACCESS"
SEARCH_PROVIDER_ENV = "FPULSE_WEB_SEARCH_PROVIDER"
SEARCH_API_KEY_ENV = "FPULSE_WEB_SEARCH_API_KEY"
SEARCH_ENDPOINT_ENV = "FPULSE_WEB_SEARCH_ENDPOINT"

# Keys under admin_settings that mirror the env vars. The Settings UI writes
# these; reads prefer them over the env so an operator can flip web access on
# without touching the process environment.
SETTING_ENABLED = "ai_web_access"
SETTING_PROVIDER = "web_search_provider"
SETTING_API_KEY = "web_search_api_key"
SETTING_ENDPOINT = "web_search_endpoint"


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def read_admin_web_settings() -> dict:
    """Return the web-access block of admin_settings ({} on any failure).

    Deliberately defensive — a missing db / settings row / parse error must
    never break the agent loop, it just means "web access off unless the env
    var says otherwise".
    """
    try:
        from fpulse.main import app_state
        db = app_state.get("db")
        if not db:
            return {}
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        if not row:
            return {}
        data = json.loads(row["data"]) if row["data"] else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def web_access_enabled() -> bool:
    """True iff web access is on — via the admin setting OR the env var.

    Either source enables it, so a headless deploy can force it on with the
    env var while a UI operator can toggle it without a restart.
    """
    if _truthy(os.environ.get(WEB_ACCESS_ENV, "")):
        return True
    return _truthy(read_admin_web_settings().get(SETTING_ENABLED, False))


def get_search_config() -> tuple[str, str, str]:
    """Resolve (provider, api_key, endpoint) for web_search.

    Admin setting wins over env for each field, so the Settings UI is the
    source of truth once used, but an env-only deploy still works.
    """
    settings = read_admin_web_settings()
    provider = str(settings.get(SETTING_PROVIDER) or os.environ.get(SEARCH_PROVIDER_ENV, "")).strip().lower()
    api_key = str(settings.get(SETTING_API_KEY) or os.environ.get(SEARCH_API_KEY_ENV, "")).strip()
    endpoint = str(settings.get(SETTING_ENDPOINT) or os.environ.get(SEARCH_ENDPOINT_ENV, "")).strip()
    return provider, api_key, endpoint


__all__ = [
    "WEB_ACCESS_ENV",
    "SEARCH_PROVIDER_ENV",
    "SEARCH_API_KEY_ENV",
    "SEARCH_ENDPOINT_ENV",
    "SETTING_ENABLED",
    "SETTING_PROVIDER",
    "SETTING_API_KEY",
    "SETTING_ENDPOINT",
    "read_admin_web_settings",
    "web_access_enabled",
    "get_search_config",
]
