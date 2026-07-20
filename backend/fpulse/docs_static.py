"""Self-hosted Swagger UI / ReDoc — serve the API docs without a CDN.

FastAPI's built-in ``/docs`` and ``/redoc`` fetch the swagger-ui / redoc
JS + CSS from ``cdn.jsdelivr.net`` at runtime. That leaves the docs page
blank on any air-gapped, firewalled, or web-filtered host — e.g. Kaspersky
Web Anti-Virus returns HTTP 503 for the CDN, so ``SwaggerUIBundle`` is
never defined and the page renders white.

F-Pulse is meant to run offline / sovereign, so the API docs must not
depend on an external network. We vendor the assets under
``fpulse/static/swagger-ui/`` and serve everything same-origin.

Vendored files (see that directory):
    swagger-ui-bundle.js, swagger-ui.css, redoc.standalone.js, favicon.png
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.staticfiles import StaticFiles

#: Directory holding the vendored swagger-ui / redoc bundles.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static", "swagger-ui")

#: URL prefix the bundles are mounted under (same-origin, no CDN).
MOUNT_PATH = "/static/swagger-ui"


def assets_present(static_dir: str = STATIC_DIR) -> bool:
    """True when the vendored Swagger UI bundle is on disk.

    Used as the guard before registering the docs routes so a stripped /
    mis-packaged install fails closed (docs 404) rather than crashing the
    whole app at import time.
    """
    return os.path.isfile(os.path.join(static_dir, "swagger-ui-bundle.js"))


def mount_self_hosted_docs(
    app: FastAPI,
    static_dir: str = STATIC_DIR,
    mount_path: str = MOUNT_PATH,
) -> None:
    """Mount the vendored assets and register same-origin docs routes.

    Registers ``/docs`` (Swagger UI), ``/redoc`` (ReDoc) and the Swagger
    OAuth2 redirect helper, all pointing at ``mount_path`` rather than a
    CDN. ``with_google_fonts=False`` keeps ReDoc from fetching
    ``fonts.googleapis.com``.

    Call this only when API docs are enabled and :func:`assets_present`
    returns True. It must run **before** any catch-all ``StaticFiles``
    mount at ``"/"`` so the asset mount wins route precedence.
    """
    # Robust + idempotent regardless of how the app was constructed: drop
    # any pre-existing /docs, /redoc or oauth2-redirect routes (e.g.
    # FastAPI's default CDN-backed ones) so ours are the only handlers.
    reserved = {"/docs", "/redoc", app.swagger_ui_oauth2_redirect_url}
    app.router.routes = [
        r for r in app.router.routes if getattr(r, "path", None) not in reserved
    ]

    app.mount(
        mount_path,
        StaticFiles(directory=static_dir),
        name="swagger-ui-assets",
    )

    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html():  # pragma: no cover - thin wrapper
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
            swagger_js_url=f"{mount_path}/swagger-ui-bundle.js",
            swagger_css_url=f"{mount_path}/swagger-ui.css",
            swagger_favicon_url=f"{mount_path}/favicon.png",
        )

    @app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
    async def swagger_ui_redirect():  # pragma: no cover - thin wrapper
        return get_swagger_ui_oauth2_redirect_html()

    @app.get("/redoc", include_in_schema=False)
    async def custom_redoc_html():  # pragma: no cover - thin wrapper
        return get_redoc_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - ReDoc",
            redoc_js_url=f"{mount_path}/redoc.standalone.js",
            redoc_favicon_url=f"{mount_path}/favicon.png",
            with_google_fonts=False,
        )
