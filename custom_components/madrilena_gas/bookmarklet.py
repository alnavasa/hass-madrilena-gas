"""Bookmarklet generator for the Madrileña Red de Gas ingest flow.

Same architecture as Canal's bookmarklet but the JS is much simpler:
the portal renders the consumption history as plain HTML at
``/consumos?page=N`` and serves it under the user's logged-in session
cookie. There's no form to submit, no periodicidad to flip, no CSV to
download — just walk the pagination, POST the concatenated HTML.

The whole thing runs against the user's own browser cookies. The 2FA
email + the rotating Laravel session lifetime are entirely sidestepped:
the bookmarklet only fires on a click while the user is already
logged in.
"""

from __future__ import annotations

from urllib.parse import quote

from .const import BOOKMARKLET_PAGE_URL_PREFIX, INGEST_URL_PREFIX

#: Readable bookmarklet body. Placeholders ``__HA_URL__``, ``__ENTRY_ID__``,
#: ``__TOKEN__``, ``__INSTALL__``, ``__INGEST_PREFIX__`` are substituted
#: at build time. Comments live in this Python docstring; inline JS
#: comments would survive into the minified one-liner and silently
#: swallow code (see Canal's _minify rationale).
_BOOKMARKLET_TEMPLATE = r"""
(async () => {
  const HA_URL = "__HA_URL__";
  const ENTRY = "__ENTRY_ID__";
  const TOKEN = "__TOKEN__";
  const INSTALL = "__INSTALL__";
  const INGEST = HA_URL.replace(/\/+$/, "") + "__INGEST_PREFIX__/" + ENTRY;
  const log = (m) => { try { console.log("[Madrileña→HA]", m); } catch (_) {} };
  const fail = (m) => { alert("❌ Madrileña → HA (" + INSTALL + ")\n\n" + m); };
  try {
    if (!location.hostname.endsWith("madrilena.es")) {
      fail("Estás en " + location.hostname + ".\n\nAbre primero https://ov.madrilena.es y vuelve a pulsar el favorito.");
      return;
    }
    log("Fetching /consumos pages…");
    const pages = [];
    for (let page = 1; page <= 20; page++) {
      const r = await fetch("/consumos?page=" + page, { credentials: "include", redirect: "manual" });
      if (r.type === "opaqueredirect" || r.status === 302 || r.status === 401) {
        fail("Sesión caducada. Vuelve a entrar a la oficina virtual y pulsa de nuevo.");
        return;
      }
      if (!r.ok) {
        if (page === 1) { fail("Página /consumos devolvió HTTP " + r.status + "."); return; }
        break;
      }
      const html = await r.text();
      if (!/<table/i.test(html) || !/\d{2}\/\d{2}\/\d{4}/.test(html)) {
        if (page === 1) { fail("La página /consumos no contiene tabla de lecturas."); return; }
        break;
      }
      pages.push(html);
      const dates = (html.match(/\d{2}\/\d{2}\/\d{4}/g) || []).length;
      log("Page " + page + ": " + dates + " date strings");
      if (dates < 5) break;
    }
    if (!pages.length) { fail("No se han podido recuperar páginas de /consumos."); return; }
    log("Posting " + pages.length + " page(s) to HA " + INGEST);
    const r4 = await fetch(INGEST, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN },
      body: JSON.stringify({
        pages_html: pages,
        client_ts: new Date().toISOString(),
        portal_url: location.href
      }),
    });
    let body = null;
    try { body = await r4.json(); } catch (_) { body = { detail: await r4.text() }; }
    if (r4.ok) {
      alert("✅ Madrileña → HA (" + INSTALL + ")\n\n" +
            "Contador: " + (body.meter_id || "—") + "\n" +
            "Lecturas importadas: " + (body.imported || 0) + "\n" +
            "Nuevas: " + (body.new || 0) + "\n" +
            "Última lectura: " + (body.last_reading_date || "—") + " → " + (body.last_reading_m3 || "—") + " m³");
    } else {
      fail("HTTP " + r4.status + " — " + (body.code || "error") + "\n\n" + (body.detail || "(sin detalle)"));
    }
  } catch (e) {
    fail("Excepción: " + (e && e.message ? e.message : e));
  }
})();
""".strip()


def _minify(src: str) -> str:
    """Single-line minify. Drops pure-comment lines so the joiner doesn't
    produce a runaway ``//`` comment that swallows the rest of the script.
    """
    out_lines: list[str] = []
    for line in src.splitlines():
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        out_lines.append(s)
    joined = " ".join(out_lines)
    while "  " in joined:
        joined = joined.replace("  ", " ")
    return joined


def build_bookmarklet(*, ha_url: str, entry_id: str, token: str, installation_name: str) -> str:
    """Return a ``javascript:…`` URL ready to paste into a bookmark."""
    ha_url = (ha_url or "").rstrip("/")
    body = (
        _minify(_BOOKMARKLET_TEMPLATE)
        .replace("__HA_URL__", _js_string_safe(ha_url))
        .replace("__ENTRY_ID__", _js_string_safe(entry_id))
        .replace("__TOKEN__", _js_string_safe(token))
        .replace("__INSTALL__", _js_string_safe(installation_name))
        .replace("__INGEST_PREFIX__", _js_string_safe(INGEST_URL_PREFIX))
    )
    return "javascript:" + quote(body, safe="(){}[]=;,:!?+-*/&|<>'.\"")


def build_bookmarklet_source(
    *, ha_url: str, entry_id: str, token: str, installation_name: str
) -> str:
    """Return the readable JS body (no ``javascript:`` prefix)."""
    ha_url = (ha_url or "").rstrip("/")
    return (
        _BOOKMARKLET_TEMPLATE.replace("__HA_URL__", _js_string_safe(ha_url))
        .replace("__ENTRY_ID__", _js_string_safe(entry_id))
        .replace("__TOKEN__", _js_string_safe(token))
        .replace("__INSTALL__", _js_string_safe(installation_name))
        .replace("__INGEST_PREFIX__", _js_string_safe(INGEST_URL_PREFIX))
    )


def _js_string_safe(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def bookmarklet_page_url(entry_id: str, token: str) -> str:
    """Relative URL of the install page served by ``BookmarkletPageView``."""
    return f"{BOOKMARKLET_PAGE_URL_PREFIX}/{entry_id}?t={quote(token, safe='')}"


def render_bookmarklet_page(
    *,
    install: str,
    ha_url: str,
    entry_id: str,
    token: str,
    bookmarklet: str,
    source: str,
) -> str:
    """Render a minimal install page: drag-link + copy button + raw source.

    Stripped-down version of Canal's page; same structure, no LAN/external
    variants in v0.1 (the user can wire that up later if they need it).
    """
    import html as _html

    install_e = _html.escape(install)
    ha_url_e = _html.escape(ha_url) if ha_url else "(no configurada)"
    entry_id_e = _html.escape(entry_id)
    token_e = _html.escape(token)
    bm_attr = _html.escape(bookmarklet, quote=True)
    source_e = _html.escape(source)
    endpoint_e = _html.escape(f"{ha_url}/api/madrilena_gas/ingest/{entry_id}" if ha_url else "(no configurada)")

    return (
        _PAGE_TEMPLATE
        .replace("__INSTALL__", install_e)
        .replace("__BOOKMARKLET__", bm_attr)
        .replace("__SOURCE__", source_e)
        .replace("__HA_URL__", ha_url_e)
        .replace("__ENTRY_ID__", entry_id_e)
        .replace("__TOKEN__", token_e)
        .replace("__ENDPOINT__", endpoint_e)
    )


_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Madrileña → HA · __INSTALL__</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#1f2328; --muted:#57606a; --accent:#0969da; --accent-fg:#fff; --ok:#1f883d; --code-bg:#f6f8fa; --border:#d0d7de; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --accent:#2f81f7; --ok:#3fb950; --code-bg:#161b22; --border:#30363d; } }
  html, body { background: var(--bg); color: var(--fg); }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem 4rem; line-height: 1.55; }
  h1 { margin-top: 0; }
  .muted { color: var(--muted); font-size: .9rem; }
  code { background: var(--code-bg); padding: .1rem .4rem; border-radius: 4px; font-size: .9em; }
  pre { background: var(--code-bg); padding: 1rem; border-radius: 6px; overflow-x: auto; border: 1px solid var(--border); font-size: .85rem; white-space: pre; }
  .variant { border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin: 1rem 0; }
  .drag-link { display: inline-block; padding: .6rem 1rem; background: var(--accent); color: var(--accent-fg); text-decoration: none; border-radius: 6px; font-weight: 600; cursor: grab; user-select: none; }
  .copy-btn { appearance: none; background: var(--accent); color: var(--accent-fg); border: none; padding: .6rem 1rem; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 1rem; }
  .copy-btn.ok { background: var(--ok); }
  details { margin: 1rem 0; }
  summary { cursor: pointer; font-weight: 600; }
  table.kv { border-collapse: collapse; width: 100%; font-size: .9rem; }
  table.kv td { padding: .4rem .5rem; border-bottom: 1px solid var(--border); vertical-align: top; word-break: break-all; }
  table.kv td:first-child { font-weight: 600; width: 9rem; color: var(--muted); word-break: normal; }
</style>
</head>
<body>
<h1>Madrileña → HA · __INSTALL__</h1>
<p>El bookmarklet conecta la <strong>Oficina Virtual de Madrileña Red de Gas</strong> con tu Home Assistant.</p>

<section class="variant">
  <p><strong>A) Arrastra a la barra de favoritos:</strong></p>
  <a class="drag-link" href="__BOOKMARKLET__" draggable="true" data-bookmarklet="__BOOKMARKLET__">★ Madrileña → HA · __INSTALL__</a>
  <p class="muted">Pulsa con el botón izquierdo y <strong>arrastra</strong> hasta la barra de marcadores. Si lo pulsas aquí no hará nada útil.</p>
  <p><strong>B) Copia y pega:</strong></p>
  <button class="copy-btn" type="button" data-bookmarklet="__BOOKMARKLET__">📋 Copiar bookmarklet</button>
  <p class="muted">Crea un favorito cualquiera, edita su URL y pega.</p>
</section>

<details><summary>Cómo usarlo</summary>
<ol>
  <li>Abre <a href="https://ov.madrilena.es" target="_blank" rel="noopener">ov.madrilena.es</a> y entra (DNI + contraseña + 2FA email).</li>
  <li>Ve a <strong>Histórico de lecturas</strong>.</li>
  <li>Pulsa el favorito. Verás un alert con el resumen.</li>
  <li>Vuelve a HA: los sensores y el panel <em>Energía → Gas</em> se rellenan solos.</li>
</ol>
</details>

<details><summary>Datos técnicos</summary>
<table class="kv">
  <tr><td>URL HA</td><td><code>__HA_URL__</code></td></tr>
  <tr><td>Entry ID</td><td><code>__ENTRY_ID__</code></td></tr>
  <tr><td>Token</td><td><code>__TOKEN__</code></td></tr>
  <tr><td>Endpoint</td><td><code>__ENDPOINT__</code></td></tr>
</table>
</details>

<details><summary>Código JavaScript legible</summary>
<pre><code>__SOURCE__</code></pre>
</details>

<script>
(function () {
  const original = "📋 Copiar bookmarklet";
  document.querySelectorAll(".copy-btn").forEach(function (btn) {
    btn.addEventListener("click", async function () {
      const text = btn.dataset.bookmarklet || "";
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "✅ Copiado";
        btn.classList.add("ok");
        setTimeout(function () { btn.textContent = original; btn.classList.remove("ok"); }, 2500);
      } catch (e) {
        window.prompt("Copia este bookmarklet:", text);
      }
    });
  });
  document.querySelectorAll(".drag-link").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      alert("Arrastra este enlace a tu barra de marcadores.\\n\\nPulsarlo aquí no hace nada — el bookmarklet necesita ejecutarse dentro de ov.madrilena.es.");
    });
  });
})();
</script>
</body>
</html>
"""
