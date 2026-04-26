"""The Madrileña Red de Gas integration.

Bookmarklet pattern, ported from the Canal de Isabel II integration:
the user pastes a one-line ``javascript:…`` favorito into their
browser, clicks it while logged into ``ov.madrilena.es``, and the
script POSTs the rendered HTML of ``/consumos`` pages straight to an
HTTP endpoint exposed by HA. No portal scraping from HA, no captive
session cookie to keep alive, no 2FA email loop.

Setup sequence
--------------

1. ``async_setup`` (once per HA boot): register the
   :class:`MadrilenaGasIngestView` (POST endpoint), the
   :class:`MadrilenaGasBookmarkletPageView` (HTML install page) and
   two services (``refresh``, ``show_bookmarklet``).

2. ``async_setup_entry`` (once per integration entry): restore the
   per-entry :class:`ReadingStore` from disk, build the coordinator,
   forward to the sensor platform. On first setup (no meter bound
   yet) we also publish the persistent notification linking to the
   bookmarklet install page.

The integration runs perfectly fine with no readings — the wizard
finishes, the bookmarklet notification appears, and entities only
materialise after the first successful POST. That POST triggers an
``async_reload`` which re-runs ``async_setup_entry`` with data present.
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .bookmarklet import (
    bookmarklet_page_url,
    build_bookmarklet,
    build_bookmarklet_source,
)
from .bookmarklet_view import MadrilenaGasBookmarkletPageView
from .const import (
    CONF_HA_URL,
    CONF_METER_ID,
    CONF_NAME,
    CONF_TOKEN,
    DEFAULT_NAME,
    DOMAIN,
)
from .coordinator import MadrilenaGasCoordinator
from .ingest import MadrilenaGasIngestView
from .store import ReadingStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

#: Manual refresh service. The integration has no way to fetch fresh
#: data on its own (no live cookie); this just kicks the coordinator to
#: re-publish whatever the store already holds. Useful after editing
#: OptionsFlow values from a developer tool — saves an HA restart.
SERVICE_REFRESH = "refresh"
SERVICE_SHOW_BOOKMARKLET = "show_bookmarklet"
ATTR_INSTANCE = "instance"

REFRESH_SCHEMA = vol.Schema({vol.Optional(ATTR_INSTANCE): cv.string})
SHOW_BOOKMARKLET_SCHEMA = vol.Schema({vol.Optional(ATTR_INSTANCE): cv.string})

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def _publish_bookmarklet_notification(
    hass: HomeAssistant, entry: ConfigEntry,
) -> None:
    """Publish (or refresh) the install notification for one entry.

    Idempotent via ``notification_id = madrilena_gas_bookmarklet_<entry_id>``
    — re-posting replaces the previous one.
    """
    ha_url = entry.data.get(CONF_HA_URL) or ""
    token = entry.data.get(CONF_TOKEN) or ""
    install = entry.data.get(CONF_NAME) or DEFAULT_NAME

    bookmarklet = build_bookmarklet(
        ha_url=ha_url, entry_id=entry.entry_id, token=token, installation_name=install,
    )
    source = build_bookmarklet_source(
        ha_url=ha_url, entry_id=entry.entry_id, token=token, installation_name=install,
    )
    page_url = (ha_url.rstrip("/") if ha_url else "") + bookmarklet_page_url(
        entry.entry_id, token,
    )

    message = (
        f"# Bookmarklet listo · {install}\n\n"
        f"Para conectar **{install}** a la Oficina Virtual de Madrileña Red de Gas:\n\n"
        f"### A) Página de instalación con botón de copiar (recomendado)\n\n"
        f'<a href="{page_url}" target="_blank" rel="noopener">'
        f"Abrir página de instalación</a>\n\n"
        f"Esa página incluye:\n"
        f"- Un enlace que puedes **arrastrar a la barra de marcadores**.\n"
        f"- Un botón **\"📋 Copiar bookmarklet\"** (un toque en iOS).\n"
        f"- El JavaScript legible por si quieres revisarlo.\n\n"
        f"### B) Pegar manualmente\n\n"
        f"```\n{bookmarklet}\n```\n\n"
        f"### Cómo usarlo\n\n"
        f"1. Crea un favorito en tu navegador.\n"
        f"2. Edita la URL del favorito y pega el bookmarklet.\n"
        f"3. Abre [ov.madrilena.es](https://ov.madrilena.es), haz login (DNI + "
        f"contraseña + 2FA por email) y entra en **Histórico de lecturas**.\n"
        f"4. Pulsa el favorito. Verás un alert con el resumen.\n"
        f"5. Vuelve a HA — los sensores y la estadística para el panel de "
        f"Energía se rellenan solos.\n\n"
        f"### Código fuente legible\n\n"
        f"```javascript\n{source}\n```\n"
    )

    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": f"Madrileña Red de Gas — bookmarklet ({install})",
            "message": message,
            "notification_id": f"madrilena_gas_bookmarklet_{entry.entry_id}",
        },
        blocking=False,
    )


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register the HTTP views + the manual services exactly once."""
    hass.data.setdefault(DOMAIN, {})
    hass.http.register_view(MadrilenaGasIngestView(hass))
    hass.http.register_view(MadrilenaGasBookmarkletPageView(hass))

    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH):

        async def _refresh(call: ServiceCall) -> None:
            wanted = (call.data.get(ATTR_INSTANCE) or "").strip().lower()
            for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
                if not isinstance(entry_data, dict):
                    continue
                coord: MadrilenaGasCoordinator | None = entry_data.get("coordinator")
                name = (entry_data.get("name") or "").lower()
                if coord is None:
                    continue
                if wanted and wanted not in {entry_id.lower(), name}:
                    continue
                _LOGGER.info("[%s] Service refresh requested", entry_id)
                await coord.async_request_refresh()

        hass.services.async_register(DOMAIN, SERVICE_REFRESH, _refresh, schema=REFRESH_SCHEMA)

    if not hass.services.has_service(DOMAIN, SERVICE_SHOW_BOOKMARKLET):

        async def _show_bookmarklet(call: ServiceCall) -> None:
            wanted = (call.data.get(ATTR_INSTANCE) or "").strip().lower()
            for config_entry in hass.config_entries.async_entries(DOMAIN):
                name = (config_entry.data.get(CONF_NAME) or "").lower()
                if wanted and wanted not in {config_entry.entry_id.lower(), name}:
                    continue
                await _publish_bookmarklet_notification(hass, config_entry)

        hass.services.async_register(
            DOMAIN, SERVICE_SHOW_BOOKMARKLET, _show_bookmarklet,
            schema=SHOW_BOOKMARKLET_SCHEMA,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    store = ReadingStore(hass, entry.entry_id)
    await store.async_load()

    coordinator = MadrilenaGasCoordinator(hass, entry, store)
    # Soft refresh: never raise — a brand-new (empty) entry is a valid
    # state until the user clicks the bookmarklet.
    await coordinator.async_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "store": store,
        "coordinator": coordinator,
        "name": entry.data.get(CONF_NAME) or entry.title or "",
        "token": entry.data.get(CONF_TOKEN, ""),
        # Per-entry asyncio.Lock serialising the read-modify-write
        # critical section in the ingest view. Two POSTs hitting the
        # same entry within milliseconds (double-click, browser retry)
        # would otherwise race the entry-data update + store write +
        # reload. Per-entry (not global) so two entries can ingest in
        # parallel.
        "ingest_lock": asyncio.Lock(),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # First-time install: no meter bound yet → publish the install
    # notification with the bookmarklet ready to copy. Once the first
    # POST binds a meter id this branch stops firing on restart, so HA
    # reboots don't re-spam the notification.
    if not entry.data.get(CONF_METER_ID):
        await _publish_bookmarklet_notification(hass, entry)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply token/name updates in-place; reload on options changes.

    Token rotation isn't supported via the wizard, but if the user edits
    ``entry.data`` directly (for example via storage), the cached token
    needs to refresh. OptionsFlow saves trigger a reload so coordinator
    + sensors materialise with the new options.
    """
    cache = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not cache:
        return
    cache["token"] = entry.data.get(CONF_TOKEN, "")
    cache["name"] = entry.data.get(CONF_NAME) or entry.title or ""
    # OptionsFlow writes are reflected in entry.options; the coordinator
    # picks them up via its self.options property on the next refresh.
    # Reload anyway because changing climate/outdoor entities should
    # re-run async_setup_entry to rewire any future entity dependencies.
    if entry.options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wipe the persisted readings file when the entry is deleted."""
    store = ReadingStore(hass, entry.entry_id)
    try:
        await store.async_clear()
    except Exception:
        _LOGGER.exception("[%s] Failed to clear store on entry removal", entry.entry_id)
