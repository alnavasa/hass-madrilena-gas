"""Distribute a bimestral consumption period across its calendar days.

Madrileña reports one reading every ~60 days. The Energy panel in HA
expects a daily (or hourly) curve. This module turns the bimestral
delta into a per-day breakdown using two signals:

1. **HDD** (Heating Degree Days) on outdoor temperature: a day with
   mean temp 4 °C burns more gas than a day with 18 °C.

2. **Climate-on hours**: if the user's HA records the thermostat state,
   we know exactly how many hours the heating was actively calling for
   heat each day. That's a much truer proxy than HDD alone (a vacant
   house at 4 °C burns nothing).

The combined weight per day is::

    weight(day) = climate_on_hours(day) * HDD(day, base_c)

Falling back to plain HDD when climate data is absent, and to a uniform
split when neither is available (no weather, no climate — the user
should at least get *some* number in the Energy panel).

ACS is constant per day (= ``baseline.m3_per_person_day * people``),
subtracted from the period total before distributing the heating
residual by weight.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta

from .acs import AcsBaseline, acs_m3_for_period
from .models import (
    ClimateActivityHour,
    DailyShare,
    DailyWeather,
    DistributionResult,
    Period,
)

_LOGGER = logging.getLogger(__name__)


def distribute_period(
    period: Period,
    *,
    weather: list[DailyWeather] | None = None,
    climate_hours: list[ClimateActivityHour] | None = None,
    acs_baseline: AcsBaseline,
    people: int,
    hdd_base_c: float = 18.0,
) -> DistributionResult:
    """Spread one bimestral period over its days.

    Parameters
    ----------
    period
        The period bounded by two consecutive readings.
    weather
        Daily mean temperatures covering ``[period.start + 1, period.end]``.
        Missing days are treated as HDD = 0 (no heating contribution).
    climate_hours
        Hourly climate activity (0..1 fraction of "heating on" within
        each hour). Missing hours treated as 0.
    acs_baseline
        Per-person, per-day ACS m³ figure. See :mod:`acs`.
    people
        Number of people in the household.
    hdd_base_c
        HDD reference temperature; standard is 18 °C in Spain.

    Returns
    -------
    DistributionResult
        One :class:`DailyShare` per day in
        ``[period.start + 1, period.end]``. Sum across days equals
        ``period.consumption_m3`` (modulo float rounding < 1e-6 m³).
    """
    days_in_period = _days_in_period(period)
    if not days_in_period:
        return DistributionResult(period=period, fallback_uniform=True)

    # ---- ACS first (constant per day, capped at period total) ----
    acs_total = acs_m3_for_period(acs_baseline, period, people)
    acs_per_day = acs_total / len(days_in_period) if days_in_period else 0.0
    heating_residual = max(0.0, period.consumption_m3 - acs_total)

    # ---- Build per-day weight: climate_on_hours × HDD ----
    weather_by_day = {w.day: w for w in (weather or [])}
    climate_hours_by_day = _aggregate_climate_hours_by_day(climate_hours or [])

    weights: dict[date, float] = {}
    has_climate = bool(climate_hours_by_day)
    has_weather = bool(weather_by_day)

    for day in days_in_period:
        hdd = weather_by_day[day].hdd(hdd_base_c) if day in weather_by_day else 0.0
        on_hours = climate_hours_by_day.get(day, 24.0 if not has_climate else 0.0)
        # If climate data exists, use it literally; if it's missing,
        # assume the heating runs as much as outdoor temp dictates
        # (i.e. weight collapses to pure HDD).
        if has_climate:
            weights[day] = on_hours * hdd
        else:
            weights[day] = hdd

    total_weight = sum(weights.values())

    fallback = False
    if total_weight <= 0:
        # Neither HDD nor climate data: split heating residual uniformly.
        # Better than zero so the Energy panel still shows a curve.
        fallback = True
        per_day = heating_residual / len(days_in_period)
        daily = [
            DailyShare(day=d, acs_m3=acs_per_day, heating_m3=per_day, weight=1.0)
            for d in days_in_period
        ]
    else:
        daily = [
            DailyShare(
                day=d,
                acs_m3=acs_per_day,
                heating_m3=heating_residual * (weights[d] / total_weight),
                weight=weights[d],
            )
            for d in days_in_period
        ]

    return DistributionResult(
        period=period,
        daily=daily,
        acs_total_m3=acs_total,
        heating_total_m3=heating_residual,
        fallback_uniform=fallback,
    )


def _days_in_period(period: Period) -> list[date]:
    """Days *consumed* in the period: (start, end] — start excluded."""
    out = []
    cur = period.start + timedelta(days=1)
    while cur <= period.end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _aggregate_climate_hours_by_day(
    hours: list[ClimateActivityHour],
) -> dict[date, float]:
    """Sum heating fractions per calendar day → hours of heating-on per day."""
    acc: dict[date, float] = defaultdict(float)
    for h in hours:
        acc[h.hour_start.date()] += h.heating_fraction
    return dict(acc)


def build_periods(readings: list) -> list[Period]:
    """Turn a chronological list of Reading into the implied Periods.

    Readings are expected newest-first (matches the parser output).
    Returns Periods oldest-first so distribution results can be merged
    forward into long-term statistics.
    """
    if len(readings) < 2:
        return []
    sorted_old_to_new = sorted(readings, key=lambda r: r.fecha)
    periods = []
    for prev, curr in zip(sorted_old_to_new, sorted_old_to_new[1:], strict=False):
        periods.append(
            Period(
                start=prev.fecha,
                end=curr.fecha,
                start_m3=prev.lectura_m3,
                end_m3=curr.lectura_m3,
                end_tipo=curr.tipo,
            )
        )
    return periods
