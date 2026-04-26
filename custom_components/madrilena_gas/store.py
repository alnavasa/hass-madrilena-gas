"""Per-entry persistence for ingested gas readings.

Persists JSON under ``<config>/.storage/madrilena_gas.<entry_id>``.
Survives HA restart so the user doesn't have to re-bookmarklet the
moment the box reboots.

Simpler than Canal's store: bimestral readings have a single key per
date (no contract dimension), and the readings are cumulative m³
straight from the dial — no liters/baseline trick needed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import MAX_READINGS_PER_ENTRY, STORAGE_KEY_PREFIX, STORAGE_VERSION
from .models import Reading

_LOGGER = logging.getLogger(__name__)


class ReadingStore:
    """Holds the cached readings + meter id for a single config entry.

    API:

    * ``async_load()`` — restore from disk in ``async_setup_entry``.
    * ``readings`` — sorted newest-first, what sensors expect.
    * ``meter_id`` — the meter number scraped from the portal header.
    * ``async_replace()`` — called by the ingest endpoint on each POST.
    * ``async_clear()`` — wipes the file, used on entry removal.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}")
        self._readings: dict[date, Reading] = {}
        self._meter_id: str | None = None
        self._last_ingest_at: datetime | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        for row in data.get("readings", []):
            try:
                r = Reading.from_dict(row)
            except (KeyError, TypeError, ValueError):
                continue
            self._readings[r.fecha] = r
        self._meter_id = data.get("meter_id") or None
        last = data.get("last_ingest_at")
        if last:
            try:
                self._last_ingest_at = datetime.fromisoformat(str(last))
            except (TypeError, ValueError):
                self._last_ingest_at = None
        _LOGGER.debug(
            "[%s] Store loaded: %d readings, meter=%s, last_ingest=%s",
            self._entry_id, len(self._readings), self._meter_id, self._last_ingest_at,
        )

    async def async_save(self) -> None:
        await self._store.async_save(self._serialise())

    @property
    def readings(self) -> list[Reading]:
        """Newest-first, the order the parser produces."""
        return sorted(self._readings.values(), key=lambda r: r.fecha, reverse=True)

    @property
    def meter_id(self) -> str | None:
        return self._meter_id

    @property
    def last_ingest_at(self) -> datetime | None:
        return self._last_ingest_at

    async def async_replace(
        self,
        new_readings: list[Reading],
        meter_id: str | None,
        ingest_at: datetime,
    ) -> int:
        """Merge a fresh batch from the bookmarklet POST.

        Returns the number of NEW readings (not in-place updates).
        ``meter_id`` is sticky — once set, only a non-None payload from
        a subsequent POST changes it (so a partial scrape that missed
        the header doesn't wipe the previously-known meter id).
        """
        new_count = 0
        for r in new_readings:
            if r.fecha not in self._readings:
                new_count += 1
            self._readings[r.fecha] = r

        # Trim oldest if over the cap. Bimestral cadence means even
        # a 100-year window stays well below the default cap, but
        # belt-and-suspenders.
        if len(self._readings) > MAX_READINGS_PER_ENTRY:
            keys = sorted(self._readings.keys())
            for k in keys[: len(self._readings) - MAX_READINGS_PER_ENTRY]:
                del self._readings[k]

        if meter_id:
            self._meter_id = meter_id
        self._last_ingest_at = ingest_at
        await self.async_save()
        return new_count

    async def async_clear(self) -> None:
        self._readings.clear()
        self._meter_id = None
        self._last_ingest_at = None
        await self._store.async_remove()

    def _serialise(self) -> dict[str, Any]:
        return {
            "readings": [r.to_dict() for r in self.readings],
            "meter_id": self._meter_id,
            "last_ingest_at": self._last_ingest_at.isoformat() if self._last_ingest_at else None,
        }
