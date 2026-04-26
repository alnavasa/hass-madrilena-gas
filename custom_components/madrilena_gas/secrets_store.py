"""Per-entry persistence for autopilot credentials and Laravel session.

Two stores, both written with HA's :class:`Store` helper:

* :class:`CredentialsStore` — DNI + password the user entered when
  enabling autopilot. Survives HA restarts so the autopilot client can
  re-login transparently when the cookie eventually expires (or the
  user re-auths from the UI).
* :class:`SessionStore` — serialised cookie jar + CSRF token captured
  from the last successful login. Lets the autopilot fetch ``/consumos``
  without needing to log in on every HA boot.

Why two stores?
---------------
The credentials are write-once-read-rarely (only on re-auth). The
session is read-on-every-poll and rewritten when Laravel rotates
cookies. Splitting the files keeps the credentials I/O minimal and
makes a redacted diagnostics dump trivial — the secrets file is the
only one that ever needs masking.

Encryption
----------
Home Assistant has no first-class secrets encryption. Files under
``<config>/.storage/`` rely on filesystem permissions (typically
``0600`` on managed installs, owner-only). This is the same threat
model every other HA integration with passwords uses (Tuya, Tesla,
Plex, etc.) — explicitly documented in the README so the user opts
in with eyes open.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    STORAGE_SECRETS_KEY_PREFIX,
    STORAGE_SESSION_KEY_PREFIX,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class CredentialsStore:
    """DNI + password for one config entry.

    The autopilot client reads these once on login. Updates only happen
    when the user re-enters them from OptionsFlow / reauth flow (e.g.
    after a password change on the portal).
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._entry_id = entry_id
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_SECRETS_KEY_PREFIX}.{entry_id}",
        )
        self._dni: str = ""
        self._password: str = ""

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._dni = str(data.get("dni") or "")
        self._password = str(data.get("password") or "")

    async def async_save(self, *, dni: str, password: str) -> None:
        self._dni = dni
        self._password = password
        await self._store.async_save({"dni": dni, "password": password})
        _LOGGER.debug("[%s] Credentials store updated", self._entry_id)

    async def async_clear(self) -> None:
        self._dni = ""
        self._password = ""
        await self._store.async_remove()

    @property
    def dni(self) -> str:
        return self._dni

    @property
    def password(self) -> str:
        return self._password

    @property
    def has_credentials(self) -> bool:
        return bool(self._dni and self._password)


class SessionStore:
    """Persisted Laravel session for the autopilot client.

    Holds the serialised cookies and any CSRF token the client needs
    to send back. The exact shape is opaque here — the client decides
    what to round-trip via ``async_save_payload`` / ``payload``.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._entry_id = entry_id
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_SESSION_KEY_PREFIX}.{entry_id}",
        )
        self._payload: dict[str, Any] = {}

    async def async_load(self) -> None:
        self._payload = await self._store.async_load() or {}

    async def async_save_payload(self, payload: dict[str, Any]) -> None:
        self._payload = dict(payload)
        await self._store.async_save(self._payload)

    async def async_clear(self) -> None:
        self._payload = {}
        await self._store.async_remove()

    @property
    def payload(self) -> dict[str, Any]:
        return dict(self._payload)

    @property
    def has_session(self) -> bool:
        return bool(self._payload)
