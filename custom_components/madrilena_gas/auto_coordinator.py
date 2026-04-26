"""Background poller for the autopilot mode.

Lives alongside the regular :class:`MadrilenaGasCoordinator` (which
just munges already-stored readings). This one actually fetches new
data from the portal:

* Every ``AUTOPILOT_POLL_INTERVAL`` (40 min by default), call
  :func:`MadrilenaClient.fetch_consumos_pages`, run the same
  :func:`parser.parse_pages` the bookmarklet ingest endpoint uses,
  feed the :class:`ReadingStore`, and kick the regular coordinator.
* On :class:`SessionExpired`, kill the loop, clear the session store,
  and start a re-auth flow so the user is prompted (in HA UI) to enter
  a fresh OTP.
* On transient errors, back off for ``AUTOPILOT_BACKOFF_INTERVAL`` and
  retry — the portal occasionally returns 5xx during off-hours
  maintenance windows.

Everything here is a SCAFFOLD: the client methods raise
:class:`MadrilenaClientNotImplemented` until the HAR capture lands.
The loop will surface that as a one-shot reauth prompt, which lets
the rest of the autopilot UI (Options toggle, reauth flow) be
exercised end-to-end with mocks.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    AUTOPILOT_BACKOFF_INTERVAL,
    AUTOPILOT_POLL_INTERVAL,
    DOMAIN,
)
from .coordinator import MadrilenaGasCoordinator
from .madrilena_client import (
    InvalidCredentials,
    InvalidOtp,
    MadrilenaClient,
    MadrilenaClientError,
    MadrilenaClientNotImplemented,
    SessionExpired,
    SessionPayload,
)
from .parser import parse_meter_id, parse_pages
from .secrets_store import CredentialsStore, SessionStore
from .store import ReadingStore

_LOGGER = logging.getLogger(__name__)


class AutoFetchCoordinator:
    """Background task that pulls /consumos on a fixed cadence.

    Owns its own :class:`aiohttp.ClientSession` and
    :class:`MadrilenaClient`. Lifecycle is bound to the config entry —
    started from ``async_setup_entry`` when autopilot is enabled,
    stopped from ``async_unload_entry``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        regular_coordinator: MadrilenaGasCoordinator,
        store: ReadingStore,
        session_store: SessionStore,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.regular_coordinator = regular_coordinator
        self.store = store
        self.session_store = session_store

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._http: aiohttp.ClientSession | None = None
        self._client: MadrilenaClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = self.hass.async_create_background_task(
            self._run(), name=f"madrilena_gas autopilot {self.entry.entry_id}",
        )
        _LOGGER.info("[%s] Autopilot started", self.entry.entry_id)

    async def async_stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None
        self._client = None
        _LOGGER.info("[%s] Autopilot stopped", self.entry.entry_id)

    async def async_request_immediate_fetch(self) -> None:
        """Skip the next sleep — useful right after a successful re-login."""
        # Implementation: when the run-loop is waiting on _stop with a
        # timeout, set+clear it to abort the wait without ending the
        # loop. For the scaffold we just log; the loop will pick up the
        # new session at the next poll.
        _LOGGER.debug("[%s] Immediate fetch requested (scaffold no-op)", self.entry.entry_id)

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Forever loop. Cancellation via :meth:`async_stop` is the exit."""
        self._http = aiohttp.ClientSession()
        self._client = MadrilenaClient(self._http)
        await self.session_store.async_load()

        while not self._stop.is_set():
            interval = AUTOPILOT_POLL_INTERVAL
            try:
                await self._tick()
            except MadrilenaClientNotImplemented as exc:
                _LOGGER.warning(
                    "[%s] Autopilot not yet implemented: %s — waiting for HAR capture",
                    self.entry.entry_id, exc,
                )
                # Don't spam the log every 40 min; idle a long while.
                interval = AUTOPILOT_POLL_INTERVAL * 6
            except SessionExpired:
                _LOGGER.info(
                    "[%s] Autopilot session expired — triggering re-auth",
                    self.entry.entry_id,
                )
                await self.session_store.async_clear()
                await self._trigger_reauth()
                # Stop polling until the user completes re-auth (which
                # will restart this coordinator via entry reload).
                return
            except (InvalidCredentials, InvalidOtp) as exc:
                _LOGGER.warning(
                    "[%s] Autopilot login rejected: %s", self.entry.entry_id, exc,
                )
                await self._trigger_reauth()
                return
            except MadrilenaClientError:
                _LOGGER.exception(
                    "[%s] Autopilot tick failed — backing off",
                    self.entry.entry_id,
                )
                interval = AUTOPILOT_BACKOFF_INTERVAL
            except Exception:
                _LOGGER.exception(
                    "[%s] Unexpected autopilot failure — backing off",
                    self.entry.entry_id,
                )
                interval = AUTOPILOT_BACKOFF_INTERVAL

            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=interval.total_seconds(),
                )
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        """One poll cycle. Raises on any unrecoverable failure."""
        if self._client is None:
            return

        payload = await self._ensure_live_session()

        pages_html = await self._client.fetch_consumos_pages(payload)
        if not pages_html:
            _LOGGER.debug("[%s] Autopilot fetched 0 pages", self.entry.entry_id)
            return

        readings = parse_pages(pages_html)
        meter_id: str | None = None
        for html in pages_html:
            meter_id = parse_meter_id(html)
            if meter_id:
                break
        if not readings or not meter_id:
            _LOGGER.warning(
                "[%s] Autopilot scrape parsed empty (pages=%d) — possible layout change",
                self.entry.entry_id, len(pages_html),
            )
            return

        now = datetime.now(UTC)
        new_count = await self.store.async_replace(readings, meter_id, ingest_at=now)
        await self.regular_coordinator.async_request_refresh()
        _LOGGER.info(
            "[%s] Autopilot tick OK — pages=%d total=%d new=%d",
            self.entry.entry_id, len(pages_html), len(readings), new_count,
        )

    # ------------------------------------------------------------------
    # Session lifecycle (silent self-heal + reauth fallback)
    # ------------------------------------------------------------------

    async def _ensure_live_session(self) -> SessionPayload:
        """Return a known-good session, doing a silent re-login if needed.

        Order of preference:
          1. Cached session payload — if still alive, use it.
          2. Stored credentials — try ``begin_login``; if the portal
             trusts this device (no MFA), persist the new session and
             continue without bothering the user.
          3. Otherwise raise :class:`SessionExpired` so ``_run`` surfaces
             a reauth notification.
        """
        if self._client is None:
            raise SessionExpired("Client not initialised")

        # 1. Try the cached cookie.
        if self.session_store.has_session:
            payload = SessionPayload(**self.session_store.payload)
            if await self._client.is_session_alive(payload):
                return payload
            _LOGGER.info(
                "[%s] Autopilot session dead — attempting silent re-login",
                self.entry.entry_id,
            )
            await self.session_store.async_clear()

        # 2. Silent re-login from stored credentials.
        creds = CredentialsStore(self.hass, self.entry.entry_id)
        await creds.async_load()
        if not creds.has_credentials:
            raise SessionExpired("No credentials stored — user must re-auth")

        ctx = await self._client.begin_login(creds.dni, creds.password)
        if ctx.needs_otp:
            # We can't read the user's email — bail to reauth UI.
            raise SessionExpired("MFA required — user must complete reauth")
        new_payload = ctx.session_payload
        await self.session_store.async_save_payload(new_payload.to_dict())
        _LOGGER.info(
            "[%s] Autopilot silently re-logged in (no MFA needed)",
            self.entry.entry_id,
        )
        return new_payload

    # ------------------------------------------------------------------
    # Re-auth glue
    # ------------------------------------------------------------------

    async def _trigger_reauth(self) -> None:
        """Ask HA to surface the reauth flow for this entry.

        The reauth flow lives in :mod:`config_flow`; HA shows a
        "Reconfigurar" button on the integration card and a discovery-
        style notification.
        """
        try:
            self.entry.async_start_reauth(self.hass)
        except Exception:
            _LOGGER.exception(
                "[%s] Failed to start reauth flow", self.entry.entry_id,
            )

        # Belt-and-suspenders persistent notification — some users
        # don't see the integration card right away.
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": (
                        f"Madrileña Red de Gas — sesión perdida ({self.entry.title})"
                    ),
                    "message": (
                        "El autopilot ha perdido la sesión con ov.madrilena.es. "
                        "Ve a **Ajustes → Dispositivos y servicios** y pulsa "
                        "**Reconfigurar** sobre la integración para volver a "
                        "iniciar sesión (te llegará un OTP por email en ese momento).\n\n"
                        "Mientras tanto puedes seguir usando el bookmarklet manual."
                    ),
                    "notification_id": (
                        f"{DOMAIN}_autopilot_reauth_{self.entry.entry_id}"
                    ),
                },
                blocking=False,
            )
        except Exception:
            _LOGGER.exception(
                "[%s] Failed to publish autopilot reauth notification",
                self.entry.entry_id,
            )
