#!/usr/bin/env python3
"""Standalone probe of the Madrileña Red de Gas login flow.

PURPOSE
-------
The HACS integration's autopilot mode needs to know:

    * the URL of every redirect in the login flow,
    * the names of every <input> in every form,
    * the names of every cookie the portal sets,
    * the URL of the OTP submission and the consumos endpoint.

This script asks YOU (interactively) for your DNI, password and the
OTP that arrives by email, walks the flow, and prints **only the
structural metadata** above. It NEVER prints:

    * cookie values, header values, request bodies, response bodies,
    * the DNI you typed, the password, or the OTP,
    * any HTML content beyond <input>/<form> tag names and attributes,
    * the full URL of authenticated pages (only path + status),
    * personal data parsed from the rendered pages.

You read the printed output, paste it into the chat, and we use it to
fill in ``custom_components/madrilena_gas/madrilena_client.py`` without
ever seeing your credentials.

USAGE
-----
    python3 probe_login.py

You'll be asked for:
    1. DNI / NIE          (typed via getpass — not echoed to the terminal)
    2. Portal password    (typed via getpass)
    3. OTP code by email  (typed via getpass — wait until it arrives)

If at any point you feel uneasy, hit Ctrl-C — the script never writes
anything to disk and only talks to ``ov.madrilena.es`` over HTTPS.

DEPENDENCIES
------------
Python stdlib only (urllib, http.cookiejar, html.parser, getpass).
No pip install required.
"""

from __future__ import annotations

import getpass
import re
import ssl
import sys
import urllib.error
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib import parse, request

PORTAL_BASE = "https://ov.madrilena.es"
USER_AGENT = "madrilena-gas-probe/0.1 (https://github.com/alnavasa/hass-madrilena-gas)"

# Conservative TLS context — same as urllib's default, written explicit
# so reviewers can see we don't disable verification.
_SSL_CTX = ssl.create_default_context()


# ----------------------------------------------------------------------
# HTML inspection — name-only, never values
# ----------------------------------------------------------------------


class _FormScraper(HTMLParser):
    """Pulls forms + inputs from an HTML document.

    Only retains attribute *names*, never user-visible text or value
    attributes (which could contain PII like the user's name).
    """

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self._current_form: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "form":
            self._current_form = {
                "action": attr.get("action") or "(same URL)",
                "method": (attr.get("method") or "GET").upper(),
                "id": attr.get("id") or "",
                "inputs": [],
            }
            self.forms.append(self._current_form)
        elif tag in ("input", "select", "textarea") and self._current_form is not None:
            name = attr.get("name") or "(unnamed)"
            input_type = attr.get("type") or "(default)"
            # Capture value ONLY for CSRF tokens (`_token`, `csrf_token`,
            # `__RequestVerificationToken`). Those are random server-side
            # strings, not PII. Everything else stays redacted.
            value = None
            if name in ("_token", "csrf_token", "__RequestVerificationToken"):
                value = attr.get("value")
            self._current_form["inputs"].append(
                {
                    "tag": tag,
                    "name": name,
                    "type": input_type,
                    "value": value,
                },
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current_form = None


def _scrape_forms(html: str) -> list[dict]:
    parser = _FormScraper()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.forms


def _count_table_rows(html: str, table_id_hint: str | None = None) -> int:
    """Count <tr> elements without reading their content.

    A blunt regex is fine here — we only want a number, not the data.
    """
    if table_id_hint:
        block_match = re.search(
            rf'<table[^>]*id\s*=\s*"{re.escape(table_id_hint)}".*?</table>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if block_match:
            return len(re.findall(r"<tr[\s>]", block_match.group(0), re.IGNORECASE))
    return len(re.findall(r"<tr[\s>]", html, re.IGNORECASE))


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------


def _build_opener(jar: CookieJar) -> request.OpenerDirector:
    return request.build_opener(
        request.HTTPCookieProcessor(jar),
        request.HTTPSHandler(context=_SSL_CTX),
    )


def _open(
    opener: request.OpenerDirector,
    url: str,
    *,
    data: bytes | None = None,
    method: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, str, str, dict[str, str]]:
    """Return (status, final_path, body_html, redirect_chain).

    Never returns the body to the caller in a form that we'd print —
    callers must scrape with ``_scrape_forms`` / ``_count_table_rows``.
    """
    req = request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "text/html,application/xhtml+xml")
    req.add_header("Accept-Language", "es-ES,es;q=0.9,en;q=0.8")
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    try:
        resp = opener.open(req, timeout=30)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, parse.urlparse(e.url or url).path, body, {}
    body = resp.read().decode("utf-8", errors="replace")
    final_path = parse.urlparse(resp.url).path
    return resp.status, final_path, body, {}


def _print_cookie_names(jar: CookieJar, label: str) -> None:
    names = sorted({c.name for c in jar})
    if not names:
        print(f"  {label}: (no cookies set)")
        return
    print(f"  {label}: {len(names)} cookies — names only")
    for n in names:
        print(f"    - {n}")


def _print_forms(forms: list[dict], label: str) -> None:
    if not forms:
        print(f"  {label}: (no forms found)")
        return
    print(f"  {label}: {len(forms)} form(s)")
    for i, f in enumerate(forms, 1):
        print(
            f"    [{i}] action={f['action']!r} method={f['method']} "
            f"id={f['id']!r} inputs={len(f['inputs'])}",
        )
        for inp in f["inputs"]:
            print(
                f"        - <{inp['tag']} name={inp['name']!r} type={inp['type']!r}>",
            )


def _hr(title: str) -> None:
    line = "=" * 60
    print(f"\n{line}\n{title}\n{line}")


# ----------------------------------------------------------------------
# Probe steps — fill in the URLs as you find them
# ----------------------------------------------------------------------


def main() -> int:
    print(__doc__)
    print("Press Ctrl-C at any time to abort.\n")

    jar = CookieJar()
    opener = _build_opener(jar)

    # ------------------------------------------------------------------
    # Step 1 — GET portal root, see where it redirects to.
    # ------------------------------------------------------------------
    _hr("STEP 1 — GET portal root (discovers login URL)")
    status, path, html, _ = _open(opener, PORTAL_BASE + "/")
    print(f"Final path:    {path}")
    print(f"Status code:   {status}")
    _print_cookie_names(jar, "Cookies after GET /")
    _print_forms(_scrape_forms(html), "Forms on landing page")

    # ------------------------------------------------------------------
    # Step 2 — GET the login page directly so we capture the form.
    # ------------------------------------------------------------------
    _hr("STEP 2 — GET /login")
    status, path, html, _ = _open(opener, PORTAL_BASE + "/login")
    print(f"Final path:    {path}")
    print(f"Status code:   {status}")
    _print_cookie_names(jar, "Cookies after GET /login")
    login_forms = _scrape_forms(html)
    _print_forms(login_forms, "Forms on /login")

    if not login_forms:
        print("\n⚠️  No forms found on /login — the portal layout has changed.")
        print("    Capture the actual page manually and report the URL.")
        return 1

    # ------------------------------------------------------------------
    # Step 3 — POST credentials. Asks for DNI + password.
    # ------------------------------------------------------------------
    _hr("STEP 3 — POST credentials (will ask the portal to email an OTP)")
    print("Enter your portal credentials. They are NOT echoed and NOT printed.")
    dni = getpass.getpass("  DNI / NIE        : ")
    password = getpass.getpass("  Portal password  : ")

    # Best-effort: assemble a body using the form's input names. Common
    # Laravel patterns: _token, username/email, password.
    target_form = login_forms[0]
    fields: dict[str, str] = {}
    csrf_from_form: str | None = None
    for inp in target_form["inputs"]:
        n = inp["name"]
        if n in ("_token", "csrf_token", "__RequestVerificationToken"):
            # Prefer the form's hidden value (the raw session token).
            # Fall back to the URL-decoded XSRF-TOKEN cookie.
            v = inp.get("value")
            if not v:
                csrf_cookie = next((c.value for c in jar if c.name == "XSRF-TOKEN"), "")
                v = parse.unquote(csrf_cookie) if csrf_cookie else ""
            if v:
                fields[n] = v
                csrf_from_form = v
        elif inp["type"] == "password" or n.lower() in ("password", "pwd"):
            fields[n] = password
        elif n.lower() in ("username", "user", "email", "dni", "nif", "login"):
            fields[n] = dni

    if not fields:
        print("⚠️  Could not deduce field names — fill them in manually:")
        for inp in target_form["inputs"]:
            fields[inp["name"]] = ""
        print("    Edit probe_login.py to map field names if this happens.")

    body = parse.urlencode(fields).encode()
    action = target_form["action"]
    if action.startswith("/"):
        post_url = PORTAL_BASE + action
    elif action.startswith("http"):
        post_url = action
    else:
        post_url = PORTAL_BASE + "/login"

    print(f"POSTing to:        {post_url}")
    print(f"Field names sent:  {sorted(fields.keys())}  (values redacted)")
    print(f"_token source:     {'form hidden' if csrf_from_form else 'cookie fallback'}")

    # Headers Laravel commonly checks: Origin/Referer (CSRF), and the
    # X-XSRF-TOKEN header (decoded cookie value) as a backup CSRF channel.
    xsrf_cookie = next((c.value for c in jar if c.name == "XSRF-TOKEN"), "")
    post_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": PORTAL_BASE,
        "Referer": PORTAL_BASE + "/login",
    }
    if xsrf_cookie:
        post_headers["X-XSRF-TOKEN"] = parse.unquote(xsrf_cookie)

    status, path, html, _ = _open(
        opener,
        post_url,
        data=body,
        method="POST",
        extra_headers=post_headers,
    )
    print(f"Final path:        {path}")
    print(f"Status code:       {status}")
    _print_cookie_names(jar, "Cookies after POST /login")
    post_login_forms = _scrape_forms(html)
    _print_forms(post_login_forms, "Forms after POST /login (look for the OTP form)")

    # Filter out the always-present logout form so we don't accidentally
    # log ourselves out trying to "submit the OTP".
    otp_forms = [
        f for f in post_login_forms
        if "logout" not in (f.get("action") or "").lower()
        and (f.get("id") or "").lower() != "logout-form"
    ]

    # If the portal redirected us straight to a dashboard path
    # (`/situacion-global`, `/home`, `/perfil`, ...), MFA was skipped
    # this time (trusted IP / cookie). Skip step 4 entirely.
    no_mfa = path != "/login" and not path.startswith("/otp") and not path.startswith("/2fa")

    # ------------------------------------------------------------------
    # Step 4 — POST OTP (only if MFA was actually triggered).
    # ------------------------------------------------------------------
    _hr("STEP 4 — POST OTP")
    if no_mfa:
        print(f"✅ No OTP needed — already logged in (redirected to {path}).")
        print("   The portal trusted this IP/cookie. Skipping OTP step.")
    elif not otp_forms:
        print("⚠️  No OTP form on the post-login page (only the logout form")
        print("    was visible). Either credentials were wrong, or the OTP")
        print("    flow uses JSON XHR. Check the browser DevTools Network")
        print("    tab to see what the form submit hits.")
        return 1
    else:
        print("Check your email for the OTP. Wait until it arrives.")
        otp = getpass.getpass("  OTP code         : ")

        target_form = otp_forms[0]
        otp_fields: dict[str, str] = {}
        for inp in target_form["inputs"]:
            n = inp["name"]
            if n in ("_token", "csrf_token", "__RequestVerificationToken"):
                v = inp.get("value")
                if not v:
                    csrf_cookie = next((c.value for c in jar if c.name == "XSRF-TOKEN"), "")
                    v = parse.unquote(csrf_cookie) if csrf_cookie else ""
                if v:
                    otp_fields[n] = v
            elif n.lower() in ("otp", "code", "codigo", "verification_code"):
                otp_fields[n] = otp
        if not any(v == otp for v in otp_fields.values()):
            # Fall back: blast the OTP into every text-like input.
            for inp in target_form["inputs"]:
                if inp["type"] in ("text", "number", "tel"):
                    otp_fields[inp["name"]] = otp
                    break

        body = parse.urlencode(otp_fields).encode()
        action = target_form["action"]
        if action.startswith("/"):
            post_url = PORTAL_BASE + action
        elif action.startswith("http"):
            post_url = action
        else:
            post_url = PORTAL_BASE + "/login"

        print(f"POSTing to:        {post_url}")
        print(f"Field names sent:  {sorted(otp_fields.keys())}  (values redacted)")

        xsrf_cookie = next((c.value for c in jar if c.name == "XSRF-TOKEN"), "")
        otp_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": PORTAL_BASE,
            "Referer": PORTAL_BASE + "/login",
        }
        if xsrf_cookie:
            otp_headers["X-XSRF-TOKEN"] = parse.unquote(xsrf_cookie)

        status, path, html, _ = _open(
            opener,
            post_url,
            data=body,
            method="POST",
            extra_headers=otp_headers,
        )
        print(f"Final path:        {path}")
        print(f"Status code:       {status}")
        _print_cookie_names(jar, "Cookies after POST OTP")

    # ------------------------------------------------------------------
    # Step 5 — Probe authenticated read endpoints. Try several
    # candidate paths until we find the one that actually returns
    # a consumption-history table (the bookmarklet hits /consumos in
    # the browser, but the underlying URL might be different).
    # ------------------------------------------------------------------
    _hr("STEP 5 — Probe candidate consumption endpoints")
    candidates = (
        "/consumos",
        "/lecturas",
        "/historico",
        "/historico-lecturas",
        "/historicoLecturas",
        "/cliente/consumos",
        "/cliente/lecturas",
        "/facturas",
        "/situacion-global",
    )
    found_consumo: str | None = None
    for cand in candidates:
        status, path, html, _ = _open(opener, PORTAL_BASE + cand)
        rows = _count_table_rows(html)
        forms = _scrape_forms(html)
        login_redirect = path == "/login"
        marker = " ← redirect to /login" if login_redirect else (
            f" rows={rows}" if rows else ""
        )
        print(f"  GET {cand:24s} → {status} (final: {path}){marker}")
        if not login_redirect and rows >= 5 and found_consumo is None:
            found_consumo = cand
    if found_consumo:
        print(f"\n✅ Likely consumption endpoint: {found_consumo}")
    else:
        print("\n⚠️  No candidate returned a non-trivial table.")
        print("    The readings endpoint may be a JSON XHR or have a")
        print("    different URL — check browser DevTools Network tab.")

    # ------------------------------------------------------------------
    # Step 6 — sanity GET of a likely "still alive" endpoint.
    # ------------------------------------------------------------------
    _hr("STEP 6 — alive-probe candidate")
    print("Will GET /home and /perfil — note which return 200 vs 302→/login.")
    for candidate in ("/home", "/perfil"):
        status, path, _html, _ = _open(opener, PORTAL_BASE + candidate)
        print(f"  {candidate:20s} → {status} (final path: {path})")

    print(
        "\n✅ Probe finished. Copy the output above and paste it into the chat.\n"
        "   Nothing was written to disk; the cookie jar dies with this process.",
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)
