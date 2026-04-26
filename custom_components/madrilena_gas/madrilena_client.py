"""HTTP client for the Madrileña Red de Gas portal (autopilot mode).

This client is the **only** module that talks to ``ov.madrilena.es``
directly from HA. The bookmarklet flow uses the user's browser instead
and never touches this code.

Status: SCAFFOLD — most methods raise :class:`MadrilenaClientNotImplemented`
until the login flow has been captured by the standalone
``probe_login.py`` script. The interface is fixed so the rest of the
autopilot (coordinator, reauth flow, options flow) can be wired up,
tested with mocks, and shipped as a no-op until the real wire format
is filled in.

Expected final shape (subject to confirmation by the probe):

    1. ``begin_login(dni, password)``
       a. ``GET  {base}/login``                 → grab CSRF token cookie + form name
       b. ``POST {base}/login`` with credentials → portal sends OTP by email,
          responds with a "give me the OTP" page
       Returns a :class:`LoginContext` opaque to callers (carries the
       cookie jar / nonce the portal expects on the next POST).

    2. ``submit_otp(login_ctx, otp)``
       a. ``POST {base}/login/otp`` (or whatever name the form uses) → on success,
          portal sets the auth cookie and redirects to the home page.
       Returns a :class:`SessionPayload` ready to persist via :class:`SessionStore`.

    3. ``fetch_consumos_pages(session_payload)``
       a. ``GET {base}/consumos?page=1..N``     → raw HTML pages
       Same payload the bookmarklet POSTs to the ingest endpoint, so
       the existing ``parser.parse_pages`` reuses verbatim.

    4. ``is_session_alive(session_payload)``
       a. Lightweight ``GET`` of a known authenticated page → ``True``
          if 200 with the expected layout, ``False`` if redirected
          back to ``/login`` (cookie expired).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import MADRILENA_PORTAL_BASE

_LOGGER = logging.getLogger(__name__)


class MadrilenaClientError(Exception):
    """Base exception for any portal interaction failure."""


class MadrilenaClientNotImplemented(MadrilenaClientError):
    """Raised by the scaffold methods until the HAR capture lands.

    The autopilot UI may surface this verbatim — the message is the
    one the user sees in the reauth flow / persistent notification.
    Keep it actionable.
    """


class InvalidCredentials(MadrilenaClientError):
    """Username or password rejected by the portal."""


class InvalidOtp(MadrilenaClientError):
    """OTP the user typed was wrong or already expired."""


class SessionExpired(MadrilenaClientError):
    """Persisted cookie no longer accepted — full re-login required."""


@dataclass(slots=True)
class LoginContext:
    """Opaque state between :func:`begin_login` and :func:`submit_otp`.

    Exists so the OTP step can recover the same cookie jar that the
    initial POST primed (Laravel rotates the session id on login;
    losing it between the two requests would invalidate the OTP form).

    Filled in once the probe lands. ``raw`` is the catch-all the client
    uses to round-trip whatever it needs.
    """

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionPayload:
    """Persisted bag the :class:`SessionStore` round-trips to disk.

    Whatever the client needs to resume work without a fresh login —
    serialised cookies, current CSRF token, anything else the portal
    wants echoed back in subsequent requests.
    """

    cookies: dict[str, str] = field(default_factory=dict)
    csrf_token: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class MadrilenaClient:
    """Thin async wrapper around :mod:`aiohttp` for the OV portal.

    All methods are async; all I/O goes through the ``aiohttp.ClientSession``
    handed in at construction time. The session is owned by the caller
    (the autopilot coordinator) so it can be reused across polls and
    cleaned up properly on entry unload.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = MADRILENA_PORTAL_BASE,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------

    async def begin_login(self, dni: str, password: str) -> LoginContext:
        """Step 1/2: post credentials, ask portal to send OTP by email.

        TODO HAR: capture
          * GET /login → look for `<input name="_token">` (CSRF) and
            cookie set in the response (likely `XSRF-TOKEN` and
            `oficina_virtual_session` placeholders).
          * POST /login → form fields for username, password, csrf.
          * Confirm the response is a "OTP requested" page (expect a
            new form with an `otp` / `code` field name).
        """
        raise MadrilenaClientNotImplemented(
            "El cliente HTTP de Madrileña aún no está implementado. "
            "Falta el HAR del flujo de login para rellenar este paso. "
            "Mientras tanto el modo autopilot no funciona — usa el "
            "bookmarklet manual.",
        )

    async def submit_otp(self, ctx: LoginContext, otp: str) -> SessionPayload:
        """Step 2/2: complete the OTP challenge, return persisted session.

        TODO HAR: capture
          * POST /login/otp (or whatever URL the OTP form posts to).
          * On success, the response should set the long-lived auth
            cookie. Capture all cookies present after this request
            into ``SessionPayload.cookies``.
          * Note any CSRF token rotation we need to honour on later
            POSTs (probably none for plain GETs of /consumos).
        """
        raise MadrilenaClientNotImplemented(
            "Falta el HAR para rellenar la verificación de OTP.",
        )

    # ------------------------------------------------------------------
    # Authenticated requests
    # ------------------------------------------------------------------

    async def fetch_consumos_pages(self, session: SessionPayload) -> list[str]:
        """Return the raw HTML of every page of ``/consumos``.

        Format must match exactly what the bookmarklet posts — the
        existing :func:`parser.parse_pages` consumes the same payload
        without changes.

        TODO HAR: capture
          * GET /consumos → check for pagination links / hidden form.
          * Loop pages 1..N until parse_pages stops finding rows.
        """
        raise MadrilenaClientNotImplemented(
            "Falta el HAR para implementar la descarga de /consumos.",
        )

    async def is_session_alive(self, session: SessionPayload) -> bool:
        """Cheap probe — True if the cookie is still good, False otherwise.

        Implementation idea (confirm with HAR):
          * GET /home (or any page that's behind the login wall).
          * If 200 with the expected layout → alive.
          * If 302 to /login or 401/403 → dead.
        """
        raise MadrilenaClientNotImplemented(
            "Falta el HAR para implementar la comprobación de sesión.",
        )
