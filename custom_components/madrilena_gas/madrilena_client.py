"""HTTP client for the Madrileña Red de Gas portal (autopilot mode).

This client is the **only** module that talks to ``ov.madrilena.es``
directly from HA. The bookmarklet flow uses the user's browser instead
and never touches this code.

Flow captured by ``probe_login.py`` (Laravel server-rendered):

    1. ``GET  /``                  → 302 → /login. Sets two cookies:
       ``XSRF-TOKEN`` + ``oficina_virtual_session``. Renders form
       ``frmLogin`` with hidden ``_token``, ``username``, ``password``.
    2. ``POST /login`` with the form ``_token`` + credentials and the
       headers ``Origin`` + ``Referer`` + ``X-XSRF-TOKEN`` (URL-decoded
       cookie) → 302 → ``/situacion-global`` on success. Wrong CSRF
       returns 419; wrong credentials return back to ``/login`` with the
       login form again.
    3. **Trusted-device path (the common case)**: no MFA challenge —
       step 2 already authenticates and we can ``GET /consumos`` straight
       away.
    4. **Untrusted device**: portal emails an OTP and renders an OTP
       form. We follow the same submit pattern (``_token`` + headers)
       to confirm. The exact URL/field names of that form are still
       *unknown* — when MFA fires, the autopilot will surface a reauth
       prompt and we'll wire the second probe pass.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import aiohttp
from yarl import URL

from .const import MADRILENA_PORTAL_BASE

_LOGGER = logging.getLogger(__name__)

# Path the portal lands on after a successful POST /login. Treated as
# the canonical "I'm authenticated" sentinel.
_DASHBOARD_PATH = "/situacion-global"

# What we send as a browser. Some Laravel apps key off the UA — the
# probe used a custom UA and the portal accepted it, but a Firefox-ish
# UA is the safest default.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) "
    "Gecko/20100101 Firefox/124.0"
)

# Hidden CSRF token in any Laravel-rendered form.
_CSRF_INPUT_RE = re.compile(
    r'<input[^>]*name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
# Detect "still on login form" → bad creds.
_LOGIN_FORM_RE = re.compile(
    r'<form[^>]*id=["\']frmLogin["\']', re.IGNORECASE,
)
# OTP form heuristic: any form (not the always-present logout-form)
# that has a text/number/tel input named otp/code/codigo/...
_OTP_FORM_RE = re.compile(
    r'<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>',
    re.IGNORECASE | re.DOTALL,
)
_FORM_ACTION_RE = re.compile(r'action=["\']([^"\']+)["\']', re.IGNORECASE)
_FORM_ID_RE = re.compile(r'id=["\']([^"\']+)["\']', re.IGNORECASE)
_INPUT_RE = re.compile(
    r'<input\b[^>]*name=["\']([^"\']+)["\'][^>]*'
    r'(?:type=["\']([^"\']+)["\'])?',
    re.IGNORECASE,
)


class MadrilenaClientError(Exception):
    """Base exception for any portal interaction failure."""


class MadrilenaClientNotImplemented(MadrilenaClientError):
    """Raised when a flow we haven't captured yet kicks in.

    The OTP submit path is the main case: until a probe captures the
    actual MFA form URL + field names, ``submit_otp`` raises this.
    """


class InvalidCredentials(MadrilenaClientError):
    """Username or password rejected by the portal."""


class InvalidOtp(MadrilenaClientError):
    """OTP the user typed was wrong or already expired."""


class SessionExpired(MadrilenaClientError):
    """Persisted cookie no longer accepted — full re-login required."""


@dataclass(slots=True)
class LoginContext:
    """Bridge between :func:`begin_login` and :func:`submit_otp`.

    ``session_payload`` is set when MFA was *not* required — the
    config-flow / autopilot can persist it directly and skip the OTP
    step. When MFA *is* required the OTP-related fields are populated
    instead.
    """

    csrf_token: str = ""
    otp_action_url: str = ""
    otp_field_name: str = ""
    session_payload: "SessionPayload | None" = None

    @property
    def needs_otp(self) -> bool:
        return self.session_payload is None


@dataclass(slots=True)
class SessionPayload:
    """Persisted bag the :class:`SessionStore` round-trips to disk.

    We only keep the minimum the portal cares about: the session cookie
    pair and a copy of the current CSRF token (handy for any later POST
    we add — the read-only autopilot doesn't currently need it).
    """

    cookies: dict[str, str] = field(default_factory=dict)
    csrf_token: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookies": dict(self.cookies),
            "csrf_token": self.csrf_token,
            "extra": dict(self.extra),
        }


class MadrilenaClient:
    """Thin async wrapper around :mod:`aiohttp` for the OV portal.

    All I/O goes through the ``aiohttp.ClientSession`` handed in at
    construction time. The session owns the cookie jar; callers either
    seed it from a stored :class:`SessionPayload` via
    :meth:`hydrate_session` or start fresh for a login.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = MADRILENA_PORTAL_BASE,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Session hydration / export
    # ------------------------------------------------------------------

    def hydrate_session(self, payload: SessionPayload) -> None:
        """Load saved cookies into the aiohttp jar."""
        if not payload.cookies:
            return
        url = URL(self._base_url)
        self._session.cookie_jar.update_cookies(payload.cookies, response_url=url)

    def export_session(self) -> SessionPayload:
        """Snapshot the current jar into a serialisable payload.

        The jar is owned by this client (created fresh per
        ``aiohttp.ClientSession``) so it only contains cookies the
        portal has set during this exchange — safe to dump wholesale
        without filtering by domain (host-only cookies have no Domain
        attribute and would otherwise be missed).
        """
        cookies = {cookie.key: cookie.value for cookie in self._session.cookie_jar}
        csrf_cookie = cookies.get("XSRF-TOKEN", "")
        return SessionPayload(
            cookies=cookies,
            csrf_token=unquote(csrf_cookie) if csrf_cookie else None,
        )

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------

    async def begin_login(self, dni: str, password: str) -> LoginContext:
        """Do GET /login → POST /login. Returns a context that either
        carries a ready :class:`SessionPayload` (no MFA) or describes
        the OTP form to complete."""
        # Step 1 — prime cookies + grab the form's CSRF token.
        async with self._session.get(
            f"{self._base_url}/login",
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
            allow_redirects=True,
        ) as resp:
            html = await resp.text()
        form_token = _extract_csrf(html)
        if not form_token:
            raise MadrilenaClientError(
                "No se encontró el token CSRF en /login — el portal cambió",
            )

        # Step 2 — POST credentials with the headers Laravel demands.
        xsrf_cookie = self._cookie_value("XSRF-TOKEN")
        post_headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/login",
        }
        if xsrf_cookie:
            post_headers["X-XSRF-TOKEN"] = unquote(xsrf_cookie)
        body = {"_token": form_token, "username": dni, "password": password}

        async with self._session.post(
            f"{self._base_url}/login",
            data=body,
            headers=post_headers,
            allow_redirects=True,
        ) as resp:
            final_path = resp.url.path
            post_html = await resp.text()
            status = resp.status

        if status == 419:
            # Token replay / session mismatch. Treat as a bad client
            # state — caller should retry from a fresh session.
            raise MadrilenaClientError(
                "Portal devolvió 419 (CSRF) — reintentar con sesión limpia",
            )

        # Trusted-device path: portal redirected straight into the dashboard.
        if final_path == _DASHBOARD_PATH or _is_authenticated_path(final_path):
            payload = self.export_session()
            new_csrf = _extract_csrf(post_html) or payload.csrf_token
            payload.csrf_token = new_csrf
            return LoginContext(csrf_token=new_csrf or "", session_payload=payload)

        # Still on /login? Two sub-cases.
        if final_path == "/login":
            otp_form = _find_otp_form(post_html)
            if otp_form:
                # MFA required — caller must drive submit_otp next.
                action = otp_form["action"] or f"{self._base_url}/login"
                if action.startswith("/"):
                    action = self._base_url + action
                return LoginContext(
                    csrf_token=otp_form["csrf"] or form_token,
                    otp_action_url=action,
                    otp_field_name=otp_form["otp_field"],
                )
            if _LOGIN_FORM_RE.search(post_html):
                # Login form re-rendered → bad creds.
                raise InvalidCredentials("DNI o contraseña incorrectos")

        # Anything else: unknown layout, surface as plain error.
        raise MadrilenaClientError(
            f"Login terminó en path inesperado: {final_path} (status={status})",
        )

    async def submit_otp(self, ctx: LoginContext, otp: str) -> SessionPayload:
        """Submit the MFA OTP and return the persisted session.

        The exact wire format here will be confirmed the first time MFA
        actually fires for a user. The current implementation submits
        the OTP using the same Laravel pattern (``_token`` + named OTP
        field + Origin/Referer/X-XSRF-TOKEN) — that should hold, but
        until we have a probe trace we surface a clear error if the
        portal answers in an unexpected shape.
        """
        if ctx.session_payload is not None:
            # Already authenticated, no OTP needed — caller misuse, but
            # be permissive.
            return ctx.session_payload

        if not ctx.otp_action_url or not ctx.otp_field_name:
            raise MadrilenaClientNotImplemented(
                "El flujo OTP no está completamente capturado todavía. "
                "Cuando Madrileña te pida un OTP, vuelve a ejecutar "
                "probe_login.py para que el script vea el formulario.",
            )

        xsrf_cookie = self._cookie_value("XSRF-TOKEN")
        post_headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/login",
        }
        if xsrf_cookie:
            post_headers["X-XSRF-TOKEN"] = unquote(xsrf_cookie)
        body = {"_token": ctx.csrf_token, ctx.otp_field_name: otp}

        async with self._session.post(
            ctx.otp_action_url,
            data=body,
            headers=post_headers,
            allow_redirects=True,
        ) as resp:
            final_path = resp.url.path
            html = await resp.text()
            status = resp.status

        if status == 419:
            raise MadrilenaClientError(
                "Portal devolvió 419 al enviar OTP — sesión perdida",
            )
        if final_path == _DASHBOARD_PATH or _is_authenticated_path(final_path):
            payload = self.export_session()
            payload.csrf_token = _extract_csrf(html) or payload.csrf_token
            return payload
        if final_path == "/login" or _find_otp_form(html):
            raise InvalidOtp("OTP incorrecto o caducado")
        raise MadrilenaClientError(
            f"OTP submit terminó en path inesperado: {final_path} (status={status})",
        )

    # ------------------------------------------------------------------
    # Authenticated requests
    # ------------------------------------------------------------------

    async def fetch_consumos_pages(
        self, payload: SessionPayload, max_pages: int = 10,
    ) -> list[str]:
        """Return the raw HTML of every page of ``/consumos``.

        Same shape ``parser.parse_pages`` consumes for the bookmarklet
        ingest endpoint. We hydrate the cookie jar first so the caller
        can share one ``MadrilenaClient`` across login + fetch.
        """
        self.hydrate_session(payload)
        out: list[str] = []
        for page in range(1, max_pages + 1):
            url = (
                f"{self._base_url}/consumos"
                if page == 1
                else f"{self._base_url}/consumos?page={page}"
            )
            async with self._session.get(
                url,
                headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
                allow_redirects=False,
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    # Redirect to /login → cookie dead.
                    raise SessionExpired("GET /consumos redirected to login")
                if resp.status == 404 and page > 1:
                    break
                if resp.status != 200:
                    if page == 1:
                        raise MadrilenaClientError(
                            f"GET /consumos returned {resp.status}",
                        )
                    break
                html = await resp.text()
            out.append(html)
            # Stop pagination as soon as a page has fewer than the
            # typical full-page row count — the portal serves chunks of
            # roughly 12 readings, so a short page is the last one.
            row_count = html.count("<tr")
            if row_count < 5:
                break
        return out

    async def is_session_alive(self, payload: SessionPayload) -> bool:
        """Cheap probe — True if the cookie still authenticates.

        ``GET /situacion-global`` with redirects disabled: 200 means
        we're in, 302 (to /login) means the cookie expired.
        """
        self.hydrate_session(payload)
        try:
            async with self._session.get(
                f"{self._base_url}{_DASHBOARD_PATH}",
                headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
                allow_redirects=False,
            ) as resp:
                if resp.status == 200:
                    return True
                if resp.status in (301, 302, 303, 307, 308):
                    return False
                # 4xx/5xx — treat as dead so caller falls back to relogin.
                return False
        except aiohttp.ClientError:
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _cookie_value(self, name: str) -> str:
        for cookie in self._session.cookie_jar:
            if cookie.key == name:
                return cookie.value
        return ""


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _extract_csrf(html: str) -> str:
    """Return the value of any ``<input name="_token">`` in the HTML."""
    m = _CSRF_INPUT_RE.search(html)
    return m.group(1) if m else ""


def _is_authenticated_path(path: str) -> bool:
    """Heuristic: any path that isn't /login or its variants."""
    if not path or path == "/":
        return False
    if path.startswith("/login"):
        return False
    return True


def _find_otp_form(html: str) -> dict[str, str] | None:
    """Locate the OTP form in a post-login HTML page.

    Returns dict with keys ``action``, ``csrf``, ``otp_field`` if a
    plausible OTP form is present, else ``None``. Excludes the always-
    present ``logout-form``.
    """
    for m in _OTP_FORM_RE.finditer(html):
        attrs = m.group("attrs") or ""
        body = m.group("body") or ""
        form_id = ""
        id_match = _FORM_ID_RE.search(attrs)
        if id_match:
            form_id = id_match.group(1)
        if form_id == "logout-form":
            continue
        action_match = _FORM_ACTION_RE.search(attrs)
        action = action_match.group(1) if action_match else ""
        if "logout" in action.lower():
            continue

        otp_field: str = ""
        csrf = ""
        for inp in _INPUT_RE.finditer(body):
            name = inp.group(1)
            if name == "_token":
                csrf_match = re.search(
                    r'value=["\']([^"\']+)["\']', inp.group(0),
                )
                if csrf_match:
                    csrf = csrf_match.group(1)
            elif name.lower() in ("otp", "code", "codigo", "verification_code", "token"):
                otp_field = name
        if otp_field:
            return {"action": action, "csrf": csrf, "otp_field": otp_field}
    return None
