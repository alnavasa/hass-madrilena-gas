"""HTTP endpoint that receives /consumos page HTML from the bookmarklet.

The user's browser already holds a live Laravel session (cookies, 2FA
solved), so the bookmarklet fetches each ``/consumos?page=N`` page
client-side and POSTs the raw HTML here. The integration validates,
parses, persists and pokes the coordinator — no headless browser, no
session-keeping daemon, no captcha to defeat.

URL:
    POST /api/madrilena_gas/ingest/{entry_id}

Auth:
    Bearer token in the Authorization header. Generated at config-flow
    time and stored in ``entry.data[CONF_TOKEN]``. Compared with
    :func:`secrets.compare_digest` to avoid timing leaks.

Body (JSON):
    {
        "pages_html": ["<full HTML of /consumos?page=1>", ...],
        "client_ts": "2026-04-25T22:30:00Z",   # optional — diagnostic
        "portal_url": "https://ov.madrilena.es/consumos"  # optional
    }

Response (JSON):
    HTTP 200 → {"ok": true, "imported": 14, "new": 2, "meter_id": "..."}
    HTTP 4xx → {"ok": false, "code": "...", "detail": "human msg"}

METER-MIXING SAFEGUARDS
=======================

A single Madrileña account can hold more than one meter (rare for
domestic users, common for fincas with several supplies). We follow
Canal's pattern:

1. **First successful POST** auto-binds the entry to the meter id
   parsed from the page header. Stored in ``entry.data[CONF_METER_ID]``.
2. **Subsequent POSTs** must carry the same meter id — anything else
   returns HTTP 409 ``meter_mismatch`` and fires a persistent
   notification telling the user to add a separate entry per meter.
3. The page header (``Contador instalado nº NNNNNN``) is the source of
   truth — we don't trust an explicit ``meter_id`` field in the JSON
   body even if present. The HTML is what the portal actually rendered;
   metadata can lie, the rendered page can't.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    CONF_METER_ID,
    CONF_NAME,
    DEFAULT_NAME,
    DOMAIN,
    INGEST_URL_PREFIX,
    MAX_INGEST_BYTES,
)
from .parser import parse_meter_id, parse_pages

_LOGGER = logging.getLogger(__name__)


class MadrilenaGasIngestView(HomeAssistantView):
    """POST endpoint for bookmarklet uploads.

    Registered once per HA boot in ``async_setup``. Multi-tenant:
    every entry shares this view, identified by ``entry_id`` in the URL
    path. Auth is per-entry via the Bearer token.

    CORS: ``cors_allowed = True`` opts this view into HA's global
    ``KEY_ALLOW_ALL_CORS`` bucket, which wires an ``aiohttp_cors``
    preflight handler onto the route automatically. Do NOT add a
    custom ``options()`` method — aiohttp refuses two OPTIONS
    handlers on the same route.
    """

    url = f"{INGEST_URL_PREFIX}/{{entry_id}}"
    name = "api:madrilena_gas:ingest"
    requires_auth = False  # We use our own Bearer token; HA session auth is unrelated.
    cors_allowed = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        # Reject obvious garbage early to avoid touching the JSON parser.
        if request.content_length is not None and request.content_length > MAX_INGEST_BYTES:
            return _error(
                request,
                413,
                "payload_too_large",
                f"Payload exceeds {MAX_INGEST_BYTES} bytes.",
            )

        entry_data = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if not entry_data:
            return _error(
                request,
                404,
                "unknown_entry",
                "No integration entry matches this URL. Check the bookmarklet target.",
            )

        # Constant-time token check.
        provided = _extract_bearer(request)
        expected = entry_data.get("token", "")
        if not provided or not expected or not secrets.compare_digest(provided, expected):
            return _error(
                request,
                401,
                "invalid_token",
                "Bearer token missing or invalid.",
            )

        # Enforce the size cap once the body is read (Content-Length may
        # be absent for chunked transfers).
        try:
            raw_body = await request.read()
        except Exception as exc:
            return _error(request, 400, "read_failed", f"Failed to read body: {exc}")
        if len(raw_body) > MAX_INGEST_BYTES:
            return _error(
                request,
                413,
                "payload_too_large",
                f"Payload exceeds {MAX_INGEST_BYTES} bytes after read.",
            )

        try:
            payload = await request.json()
        except Exception as exc:
            return _error(request, 400, "invalid_json", f"JSON parse failed: {exc}")

        if not isinstance(payload, dict):
            return _error(request, 400, "invalid_payload", "Top-level body must be an object.")

        pages_html = payload.get("pages_html")
        if not isinstance(pages_html, list) or not pages_html:
            return _error(
                request,
                400,
                "missing_pages",
                "Field 'pages_html' is required and must be a non-empty list.",
            )
        if not all(isinstance(p, str) and p.strip() for p in pages_html):
            return _error(
                request,
                400,
                "invalid_pages",
                "Every entry in 'pages_html' must be a non-empty string.",
            )

        # ------------------------------------------------------------------
        # Parse the HTML — gives us the readings and the meter id.
        # ------------------------------------------------------------------
        readings = parse_pages(pages_html)
        if not readings:
            return _error(
                request,
                400,
                "empty_pages",
                (
                    "HTML parsed to zero readings — wrong page, expired session "
                    "(the bookmarklet captured the login page), or the table "
                    "layout changed."
                ),
            )

        # The header is on every page; first non-None wins.
        posted_meter_id: str | None = None
        for html in pages_html:
            posted_meter_id = parse_meter_id(html)
            if posted_meter_id:
                break
        if not posted_meter_id:
            return _error(
                request,
                400,
                "missing_meter_id",
                (
                    "Could not extract the meter number from the page header. "
                    "Make sure the bookmarklet captured /consumos and not the "
                    "login page."
                ),
            )

        # ------------------------------------------------------------------
        # Critical section: meter-mixing safeguard + first-ingest claim
        # + store write + reload/refresh trigger.
        #
        # Two POSTs hitting the same entry within milliseconds (double
        # click, browser retry on flaky network) would otherwise race
        # the entry-data update, the JSON store write, and the reload.
        # The per-entry asyncio.Lock (created in __init__.py) serialises
        # the whole block PER ENTRY; different entries still ingest in
        # parallel.
        # ------------------------------------------------------------------
        async with entry_data["ingest_lock"]:
            config_entry = self.hass.config_entries.async_get_entry(entry_id)
            if config_entry is None:
                return _error(request, 404, "unknown_entry", "Entry vanished mid-request.")

            expected_meter = (config_entry.data.get(CONF_METER_ID) or "").strip()
            install_name = config_entry.data.get(CONF_NAME) or DEFAULT_NAME

            if expected_meter and expected_meter != posted_meter_id:
                await _notify_meter_mismatch(
                    self.hass,
                    install_name,
                    expected_meter,
                    posted_meter_id,
                    entry_id,
                )
                return _error(
                    request,
                    409,
                    "meter_mismatch",
                    (
                        f"This integration entry ('{install_name}') is bound to meter "
                        f"{expected_meter}, but the page is for meter {posted_meter_id}. "
                        "If you have more than one meter, add a separate integration "
                        "entry for the other one."
                    ),
                )

            first_ingest = not expected_meter
            if first_ingest:
                new_data = {**config_entry.data, CONF_METER_ID: posted_meter_id}
                self.hass.config_entries.async_update_entry(config_entry, data=new_data)
                _LOGGER.info(
                    "[%s] First ingest — entry now bound to meter %s",
                    entry_id,
                    posted_meter_id,
                )

            # ------------------------------------------------------------------
            # Store + push.
            # ------------------------------------------------------------------
            store = entry_data["store"]
            coordinator = entry_data["coordinator"]
            now = datetime.now(UTC)
            new_count = await store.async_replace(readings, posted_meter_id, ingest_at=now)

            # The first ever POST creates entities — schedule a reload so
            # ``async_setup_entry`` re-runs with data present and
            # materialises the sensors. Subsequent POSTs only need a
            # coordinator refresh.
            if first_ingest:
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(entry_id),
                    name="madrilena_gas first_ingest reload",
                )
            else:
                await coordinator.async_request_refresh()

        last_reading = readings[0]  # parse_pages sorts newest-first
        _LOGGER.info(
            "[%s] Ingest OK — meter=%s pages=%d total=%d new=%d last=%s (%s m³) first=%s",
            entry_id,
            posted_meter_id,
            len(pages_html),
            len(readings),
            new_count,
            last_reading.fecha.isoformat(),
            last_reading.lectura_m3,
            "yes" if first_ingest else "no",
        )
        return _json(
            request,
            200,
            {
                "ok": True,
                "imported": len(readings),
                "new": new_count,
                "meter_id": posted_meter_id,
                "installation": install_name,
                "last_reading_date": last_reading.fecha.isoformat(),
                "last_reading_m3": last_reading.lectura_m3,
                "ingest_at": now.isoformat(),
            },
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _extract_bearer(request: web.Request) -> str:
    raw = request.headers.get("Authorization", "")
    if not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()


def _json(
    request: web.Request,
    status: int,
    body: dict[str, Any],
) -> web.Response:
    """Return a JSON response. CORS headers are attached by HA's aiohttp_cors
    middleware (we opted in via ``cors_allowed = True``).
    """
    return web.json_response(body, status=status)


def _error(
    request: web.Request,
    status: int,
    code: str,
    detail: str,
) -> web.Response:
    _LOGGER.warning("Ingest %d %s: %s", status, code, detail)
    return _json(
        request,
        status,
        {"ok": False, "code": code, "detail": detail},
    )


async def _notify_meter_mismatch(
    hass: HomeAssistant,
    install_name: str,
    expected: str,
    posted: str,
    entry_id: str,
) -> None:
    """Persistent notification on meter mismatch.

    The endpoint already returned 409 to the bookmarklet (which
    surfaces it as an alert in the user's browser), but a persistent
    notification gives the user a record they can act on later from
    the HA UI — the browser alert is dismissed instantly and the user
    may not remember the exact text.
    """
    try:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"Madrileña Red de Gas — contador no coincide ({install_name})",
                "message": (
                    f"El bookmarklet envió el contador **{posted}**, pero esta "
                    f'integración ("{install_name}") está vinculada al contador '
                    f"**{expected}**.\n\n"
                    "¿Qué hacer?\n"
                    "1. Si querías subir el contador que ya tenías configurado: "
                    "abre la Oficina Virtual, asegúrate de que el contador "
                    "mostrado es el correcto y vuelve a pulsar el bookmarklet.\n"
                    "2. Si tienes más de un contador: añade otra integración "
                    "Madrileña Red de Gas en *Ajustes → Dispositivos y servicios "
                    "→ Añadir integración*, configúrala y usa **el bookmarklet "
                    "de esa nueva integración** para subir las lecturas del "
                    "otro contador."
                ),
                "notification_id": f"madrilena_gas_meter_mismatch_{entry_id}",
            },
            blocking=False,
        )
    except Exception:
        _LOGGER.exception("Could not raise meter-mismatch notification")
