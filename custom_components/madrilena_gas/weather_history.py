"""Fetch historical daily mean temperatures from Open-Meteo Archive.

Open-Meteo's archive endpoint serves ERA5 reanalysis data for free,
with no API key. We use it once at install time to back-fill HDD for
the months covered by the user's bimestral history but predating the
HA recorder. From then on, HA's own weather entity history is enough.

The endpoint is fully async-friendly via httpx (already a dep through
the bookmarklet ingest path); we only hit it during the initial
backfill and on explicit user-triggered refreshes.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx

from .const import OPEN_METEO_ARCHIVE_URL
from .models import DailyWeather

_LOGGER = logging.getLogger(__name__)


async def fetch_daily_mean_temps(
    lat: float,
    lon: float,
    start: date,
    end: date,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 30.0,
) -> list[DailyWeather]:
    """Return one DailyWeather per day in [start, end] inclusive.

    Returns an empty list (and logs a warning) on network errors so a
    flaky Open-Meteo doesn't break setup; the integration just
    distributes affected periods uniformly.
    """
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_mean",
        "timezone": "Europe/Madrid",
    }
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.get(OPEN_METEO_ARCHIVE_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _LOGGER.warning("Open-Meteo archive fetch failed: %s", exc)
        return []
    finally:
        if own_client:
            await client.aclose()

    daily = payload.get("daily", {})
    days = daily.get("time", [])
    temps = daily.get("temperature_2m_mean", [])
    out: list[DailyWeather] = []
    for d, t in zip(days, temps, strict=False):
        if t is None:
            continue
        try:
            out.append(DailyWeather(day=date.fromisoformat(d), mean_temp_c=float(t)))
        except (ValueError, TypeError):
            continue
    return out
