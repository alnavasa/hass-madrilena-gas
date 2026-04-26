"""Parse the Madrileña Red de Gas /consumos page HTML.

The portal is server-rendered Laravel, so the readings table sits in
plain HTML. The bookmarklet POSTs the full body of one (or both) pages;
this module turns that into a clean ``list[Reading]``.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup

from .models import Reading, ReadingType

_METER_RE = re.compile(r"Contador instalado n[ºo]\s*(\d+)", re.IGNORECASE)


def parse_meter_id(html: str) -> str | None:
    """Extract the meter number from the page header. None if not found."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    m = _METER_RE.search(text)
    return m.group(1) if m else None


def parse_readings(html: str) -> list[Reading]:
    """Extract all readings from one /consumos page.

    The portal renders one ``<table>`` with rows ``<tr><td>fecha</td>
    <td>lectura</td><td>tipo</td></tr>``. The header row has ``<th>``
    cells so it's naturally skipped. Numeric format is Spanish:
    thousands as ``.``, decimals as ``,`` (most readings are integer
    m³ so this rarely matters, but the parser handles both).
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[Reading] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 3 or "/" not in cells[0]:
                continue
            try:
                fecha = datetime.strptime(cells[0], "%d/%m/%Y").date()
            except ValueError:
                continue
            lectura = _parse_es_number(cells[1])
            if lectura is None:
                continue
            out.append(Reading(fecha=fecha, lectura_m3=lectura, tipo=ReadingType.from_label(cells[2])))
    return out


def parse_pages(pages_html: list[str]) -> list[Reading]:
    """Combine multiple /consumos?page=N HTMLs into one deduped list.

    Pagination on the portal is just chronological slicing. We collect
    every page, dedup by date (keeping the first occurrence — the most
    recent page wins if the user hits page=1 last), and return sorted
    most-recent-first.
    """
    seen: set = set()
    merged: list[Reading] = []
    for html in pages_html:
        for r in parse_readings(html):
            if r.fecha in seen:
                continue
            seen.add(r.fecha)
            merged.append(r)
    merged.sort(key=lambda r: r.fecha, reverse=True)
    return merged


def _parse_es_number(s: str) -> float | None:
    """Parse a Spanish-formatted number string. ``"1.465"`` → 1465.0."""
    s = s.strip()
    if not s:
        return None
    if "," in s:
        # Has decimal part: "1.465,32" → "1465.32"
        s = s.replace(".", "").replace(",", ".")
    else:
        # Pure integer with possible thousands separator: "1.465" → "1465"
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None
