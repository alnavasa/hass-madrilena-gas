"""Push cumulative streams (total/ACS/heating + optional cost) to long-term statistics.

The Energy panel reads from the recorder's external statistics table.
We push four series per meter so the user can choose:

* ``madrilena_gas:total_<meter>`` — total m³ (heating + ACS combined)
* ``madrilena_gas:acs_<meter>`` — ACS portion only
* ``madrilena_gas:heating_<meter>`` — heating portion only
* ``madrilena_gas:cost_<meter>`` — total EUR (only if ``enable_cost`` is on)

All series are pushed cumulatively from zero and re-replayed on every
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
    cost_eur_per_m3: float | None = None,
) -> None:
    """Build and upsert all cumulative streams in one sweep.

    If ``cost_eur_per_m3`` is provided (typically
    ``kwh_per_m3 × price_eur_kwh``), an extra cost stream is pushed in
    EUR. Pass ``None`` to skip cost tracking.
    """
    if not distributions:
        return

    tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC
    total_stream, acs_stream, heating_stream = daily_to_cumulative_streams(
        distributions, tz=tz,
    )
    if not total_stream:
        return

    streams: list[tuple[str, list, str, str]] = [
        ("total", total_stream, "Consumo total", UnitOfVolume.CUBIC_METERS),
        ("acs", acs_stream, "ACS (agua caliente)", UnitOfVolume.CUBIC_METERS),
        ("heating", heating_stream, "Calefacción", UnitOfVolume.CUBIC_METERS),
    ]

    if cost_eur_per_m3 is not None and cost_eur_per_m3 > 0:
        cost_stream = [
            (start_utc, round(cum_m3 * cost_eur_per_m3, 4))
            for start_utc, cum_m3 in total_stream
        ]
        streams.append(("cost", cost_stream, "Coste total", "EUR"))

    for suffix, stream, friendly, unit in streams:
        if not stream:
            continue
        sid = statistic_id(suffix, meter_id)
        meta = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"{install_name} — {friendly}",
            source=DOMAIN,
            statistic_id=sid,
            unit_of_measurement=unit,
        )
        rows = [
            StatisticData(start=start_utc, sum=cum_value)
            for start_utc, cum_value in stream
        ]
        try:
            async_add_external_statistics(hass, meta, rows)
        except Exception:
            _LOGGER.exception("Failed to push %s (%d rows)", sid, len(rows))
        else:
            _LOGGER.debug("Pushed %s — %d rows, last=%s %s", sid, len(rows), rows[-1].get("sum"), unit)
