"""Push three cumulative streams (total/ACS/heating) to long-term statistics.

The Energy panel reads from the recorder's external statistics table.
We push three series per meter so the user can choose:

* ``madrilena_gas:total_<meter>`` — total m³ (heating + ACS combined)
* ``madrilena_gas:acs_<meter>`` — ACS portion only
* ``madrilena_gas:heating_<meter>`` — heating portion only

All three are pushed cumulatively from zero and re-replayed on every
coordinator refresh. The recorder upserts by ``(statistic_id, start)``,
so the same calendar day's bar gets corrected in-place when a fresh
bimestral reading shifts the heating/ACS split — exactly the behaviour
Canal uses for its cost statistic, and the user's mental model maps
straight across.
"""

from __future__ import annotations

import logging

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .models import DistributionResult
from .statistics_helpers import daily_to_cumulative_streams, statistic_id

_LOGGER = logging.getLogger(__name__)


async def push_distribution_streams(
    hass: HomeAssistant,
    *,
    meter_id: str,
    install_name: str,
    distributions: list[DistributionResult],
) -> None:
    """Build and upsert all three cumulative streams in one sweep."""
    if not distributions:
        return

    tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC
    total_stream, acs_stream, heating_stream = daily_to_cumulative_streams(
        distributions, tz=tz,
    )
    if not total_stream:
        return

    streams = [
        ("total", total_stream, "Consumo total"),
        ("acs", acs_stream, "ACS (agua caliente)"),
        ("heating", heating_stream, "Calefacción"),
    ]

    for suffix, stream, friendly in streams:
        if not stream:
            continue
        sid = statistic_id(suffix, meter_id)
        meta = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"{install_name} — {friendly}",
            source=DOMAIN,
            statistic_id=sid,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )
        rows = [
            StatisticData(start=start_utc, sum=cum_m3)
            for start_utc, cum_m3 in stream
        ]
        try:
            async_add_external_statistics(hass, meta, rows)
        except Exception:
            _LOGGER.exception("Failed to push %s (%d rows)", sid, len(rows))
        else:
            _LOGGER.debug("Pushed %s — %d rows, last=%s m³", sid, len(rows), rows[-1].get("sum"))
