"""Pure helpers for long-term statistics push.

Madrileña Gas publishes three external statistic series per meter:

* ``madrilena_gas:total_<meter_id>`` — total m³ consumed (heating + ACS)
* ``madrilena_gas:acs_<meter_id>`` — ACS m³ alone
* ``madrilena_gas:heating_<meter_id>`` — heating m³ alone

Each series is pushed cumulatively and **always replayed from zero**.
That's important because every fresh bimestral reading triggers a
re-distribution of the period it caps — heating weights shift slightly
as new weather/climate data arrives, the ACS baseline may move when a
new summer period lands, and the user can edit ``people`` mid-life. A
replay-from-zero push lets the recorder upsert the corrected slots
in-place via ``(statistic_id, start)`` while the older days stay
identical.

This is the same "always replay" strategy Canal uses for cost; the
spike-immunity logic is identical.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from .models import DistributionResult


def daily_to_cumulative_streams(
    distributions: list[DistributionResult],
    *,
    tz: ZoneInfo,
) -> tuple[
    list[tuple[datetime, float]],
    list[tuple[datetime, float]],
    list[tuple[datetime, float]],
]:
    """Build (total, acs, heating) cumulative streams in UTC.

    The recorder stores statistics with hour-aligned start timestamps
    in UTC. A calendar day in Madrid (Europe/Madrid) maps to a 23/24/25
    hour bucket depending on DST. We anchor each day's contribution at
    its **00:00 local → UTC** instant, so the dashboard renders one
    "bar" per civil day even across DST switches.

    Returns three parallel lists of ``(start_utc, cumulative_m3)``,
    sorted by time, ready for ``async_add_external_statistics``.
    """
    if not distributions:
        return [], [], []

    daily_total: dict[date, float] = {}
    daily_acs: dict[date, float] = {}
    daily_heating: dict[date, float] = {}

    for dist in distributions:
        for share in dist.daily:
            daily_total[share.day] = daily_total.get(share.day, 0.0) + share.total_m3
            daily_acs[share.day] = daily_acs.get(share.day, 0.0) + share.acs_m3
            daily_heating[share.day] = daily_heating.get(share.day, 0.0) + share.heating_m3

    days = sorted(daily_total.keys())
    total_stream: list[tuple[datetime, float]] = []
    acs_stream: list[tuple[datetime, float]] = []
    heating_stream: list[tuple[datetime, float]] = []
    cum_total = cum_acs = cum_heating = 0.0
    for d in days:
        local_midnight = datetime.combine(d, time(0, 0), tzinfo=tz)
        utc_midnight = local_midnight.astimezone(timezone.utc)
        cum_total += daily_total[d]
        cum_acs += daily_acs.get(d, 0.0)
        cum_heating += daily_heating.get(d, 0.0)
        total_stream.append((utc_midnight, cum_total))
        acs_stream.append((utc_midnight, cum_acs))
        heating_stream.append((utc_midnight, cum_heating))
    return total_stream, acs_stream, heating_stream


def statistic_id(suffix: str, meter_id: str) -> str:
    """Build the recorder statistic_id used by the Energy dashboard.

    Format ``<domain>:<suffix>_<meter>`` matches Canal's convention
    so the user's mental model carries over.
    """
    safe_meter = "".join(c for c in meter_id if c.isalnum()) or "unknown"
    return f"madrilena_gas:{suffix}_{safe_meter}"
