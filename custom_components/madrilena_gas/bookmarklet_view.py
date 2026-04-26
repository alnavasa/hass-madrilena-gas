"""HTML install page for the Madrileña Gas bookmarklet.

Same auth model as Canal's view: the user reaches this page by clicking
a Markdown link in a persistent notification. That click is a plain
browser navigation, so HA's normal ``requires_auth=True`` (which expects
the frontend's Bearer header) returns 401. We use ``requires_auth =
False`` and validate the per-entry token from the ``?t=<token>`` query
parameter instead — the same token already embedded inside the
bookmarklet's ``Authorization`` header, so URL exposure is symmetric.
"""

from __future__ import annotations

import html
import logging
import secrets

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .bookmarklet import (
    build_bookmarklet,
    build_bookmarklet_source,
    render_bookmarklet_page,
)
from .const import (
    BOOKMARKLET_PAGE_URL_PREFIX,
    CONF_HA_URL,
    CONF_NAME,
    CONF_TOKEN,
    DEFAULT_NAME,
)

_LOGGER = logging.getLogger(__name__)


class MadrilenaGasBookmarkletPageView(HomeAssistantView):
    url = f"{BOOKMARKLET_PAGE_URL_PREFIX}/{{entry_id}}"
    name = "api:madrilena_gas:bookmarklet_page"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        config_entry = self.hass.config_entries.async_get_entry(entry_id)
        if config_entry is None:
            return web.Response(
                status=404,
                text=(
                    "<!DOCTYPE html><meta charset=utf-8>"
                    "<title>404 — Madrileña Red de Gas</title>"
                    '<body style="font-family:-apple-system,sans-serif;'
                    'max-width:40rem;margin:3rem auto;padding:0 1rem">'
                    "<h1>404 · Entry no encontrada</h1>"
                    f"<p>No hay ninguna integración Madrileña Red de Gas con id "
                    f"<code>{html.escape(entry_id)}</code>. Quizá la borraste o "
                    f"el enlace está obsoleto.</p>"
                    "</body>"
                ),
                content_type="text/html",
                charset="utf-8",
            )

        ha_url = config_entry.data.get(CONF_HA_URL) or ""
        token = config_entry.data.get(CONF_TOKEN) or ""
        install = config_entry.data.get(CONF_NAME) or DEFAULT_NAME

        provided_token = request.query.get("t", "")
        if not token or not provided_token or not secrets.compare_digest(provided_token, token):
            return web.Response(
                status=401,
                text=(
                    "<!DOCTYPE html><meta charset=utf-8>"
                    "<title>401 — Madrileña Red de Gas</title>"
                    '<body style="font-family:-apple-system,sans-serif;'
                    'max-width:40rem;margin:3rem auto;padding:0 1rem">'
                    "<h1>401 · No autorizado</h1>"
                    "<p>Esta página requiere el token de la integración en el "
                    "query string (<code>?t=…</code>). Vuelve a la notificación "
                    '<strong>"Bookmarklet listo"</strong> y pulsa el enlace de '
                    "instalación desde ahí — ya incluye el token. Si la perdiste, "
                    "regenérala desde <em>Herramientas para desarrolladores → "
                    "Acciones → <code>madrilena_gas.show_bookmarklet</code></em>.</p>"
                    "</body>"
                ),
                content_type="text/html",
                charset="utf-8",
            )

        bookmarklet = build_bookmarklet(
            ha_url=ha_url, entry_id=entry_id, token=token, installation_name=install,
        )
        source = build_bookmarklet_source(
            ha_url=ha_url, entry_id=entry_id, token=token, installation_name=install,
        )
        body = render_bookmarklet_page(
            install=install,
            ha_url=ha_url,
            entry_id=entry_id,
            token=token,
            bookmarklet=bookmarklet,
            source=source,
        )
        return web.Response(text=body, content_type="text/html", charset="utf-8")
