"""Pull daily-mean temperature and per-hour climate activity from HA recorder.

These helpers translate raw HA state history into the typed dataclasses
the distribution algorithm consumes:

* :func:`fetch_daily_temps_from_recorder` → ``list[DailyWeather]``
* :func:`fetch_climate_hours_from_recorder` → ``list[ClimateActivityHour]``

Both run inside a recorder executor job (``recorder.get_instance().async_add_executor_job``)
because :func:`history.get_significant_states_with_session` is sync. We keep
the queries narrow (one entity at a time, single window) so we don't
starve the recorder pool.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .models import ClimateActivityHour, DailyWeather

_LOGGER = logging.getLogger(__name__)

#: Climate states + hvac_action values that count as "heating on".
#: A modern thermostat exposes ``hvac_action == "heating"`` when the
#: relay is actually closed; legacy ones only flip ``state`` to "heat".
_HEATING_STATES = {"heat", "heat_cool", "auto"}
_HEATING_ACTIONS = {"heating"}
#: For ``binary_sensor.*`` selections (e.g. Airzone "demanda de suelo")
#: we treat plain on/off as the heat-on signal — no attributes needed.
_BINARY_ON_STATES = {"on", "true", "1"}


async def fetch_daily_temps_from_recorder(
    hass: HomeAssistant,
    entity_id: str,
    start: date,
    end: date,
) -> list[DailyWeather]:
    """Daily mean of an outdoor-temperature entity for ``[start+1, end]``.

    Works with:
      * ``sensor.*`` whose state is the temperature (in °C) directly.
      * ``weather.*`` whose state is a condition string and the
        temperature lives in ``attributes['temperature']``.

    Returns one :class:`DailyWeather` per day with at least one data
    point. Days with no observations are simply omitted (the
    distribution backfills those via Open-Meteo).
    """
    if start >= end:
        return []

    tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC
    # Period semantics: consumption days are (start, end] in local tz.
    window_start = datetime.combine(start, time(0, 0), tzinfo=tz)
    window_end = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=tz)

    states_by_entity = await get_instance(hass).async_add_executor_job(
        _get_states_in_window, hass, entity_id, window_start, window_end,
    )
    states: list[State] = states_by_entity.get(entity_id, [])
    if not states:
        return []

    # Bucket samples per civil day (local tz). Use the timestamp at which
    # each state was reported; for the daily mean a simple unweighted
    # average is good enough at this granularity (every ~5 min for a
    # weather entity).
    samples: dict[date, list[float]] = defaultdict(list)
    for st in states:
        temp = _state_to_temp(st)
        if temp is None:
            continue
        local_day = st.last_updated.astimezone(tz).date()
        if local_day <= start or local_day > end:
            continue
        samples[local_day].append(temp)

    out: list[DailyWeather] = []
    for day in sorted(samples):
        values = samples[day]
        if not values:
            continue
        out.append(DailyWeather(day=day, mean_temp_c=sum(values) / len(values)))
    return out


async def fetch_climate_hours_from_recorder(
    hass: HomeAssistant,
    entity_ids: list[str],
    start: date,
    end: date,
) -> list[ClimateActivityHour]:
    """Per-hour heating fraction across one or more climate entities.

    For each hour in ``(start, end]``, the fraction is the union of any
    configured climate entity being in a heating state during that hour
    (a thermostat that runs 30 of 60 min contributes 0.5; two entities,
    one running the full hour and another the second half, still
    contribute 1.0 — we want "was heat being called for at all").

    Returned list is sparse: hours where no entity reported anything are
    omitted. The distribution treats missing hours as zero heating, which
    matches "the boiler was clearly off".
    """
    if not entity_ids or start >= end:
        return []

    tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC
    window_start = datetime.combine(start, time(0, 0), tzinfo=tz)
    window_end = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=tz)

    states_by_entity = await get_instance(hass).async_add_executor_job(
        _get_states_in_window, hass, entity_ids, window_start, window_end,
    )

    # Per hour-bucket → max fraction-on across entities.
    bucket: dict[datetime, float] = defaultdict(float)
    for entity_id in entity_ids:
        states = states_by_entity.get(entity_id, [])
        if not states:
            continue
        # Walk consecutive (state[i], state[i+1]) pairs; the duration of
        # state i is (last_updated[i+1] - last_updated[i]).
        # For the trailing state, extend it to window_end.
        spans = list(zip(states, states[1:] + [None], strict=False))
        for st, nxt in spans:
            if not _is_heating(st):
                continue
            span_start = max(st.last_updated, window_start).astimezone(tz)
            span_end_raw = nxt.last_updated if nxt is not None else window_end
            span_end = min(span_end_raw, window_end).astimezone(tz)
            if span_end <= span_start:
                continue
            _accumulate_hours(bucket, span_start, span_end)

    out: list[ClimateActivityHour] = []
    for hour_start, fraction in sorted(bucket.items()):
        capped = min(1.0, fraction)
        out.append(ClimateActivityHour(hour_start=hour_start, heating_fraction=capped))
    return out


# ----------------------------------------------------------------------
# Sync recorder bridge — runs in the recorder thread pool.
# ----------------------------------------------------------------------


def _get_states_in_window(
    hass: HomeAssistant,
    entity_or_entities: str | list[str],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, list[State]]:
    """Wrap ``history.get_significant_states`` for one or many entities."""
    entity_ids = (
        [entity_or_entities] if isinstance(entity_or_entities, str) else list(entity_or_entities)
    )
    return history.get_significant_states(
        hass,
        window_start,
        window_end,
        entity_ids=entity_ids,
        no_attributes=False,  # we need attributes for weather.* and hvac_action
        include_start_time_state=True,  # carry state over from before window_start
    )


# ----------------------------------------------------------------------
# Pure helpers (unit-testable without HA wiring).
# ----------------------------------------------------------------------


def _state_to_temp(state: State) -> float | None:
    """Extract a °C value from a sensor.* or weather.* state. None if N/A."""
    if state is None:
        return None
    if state.state in (None, "", "unknown", "unavailable"):
        # Some weather.* expose temperature in attributes even when state
        # is a condition like "sunny" — try those before giving up.
        attr_temp = state.attributes.get("temperature") if state.attributes else None
        return _safe_float(attr_temp)
    direct = _safe_float(state.state)
    if direct is not None:
        return direct
    if state.attributes:
        return _safe_float(state.attributes.get("temperature"))
    return None


def _is_heating(state: State) -> bool:
    """True if the entity's state represents 'heat is being called for now'.

    For ``climate.*``: prefer ``hvac_action`` ("heating" / "idle" /
    "off" / "cooling") which is the truest signal. Legacy thermostats
    only flip ``state`` to "heat" — fall back to that when no action.

    For ``binary_sensor.*``: plain ``on`` is heat-on. Useful when the
    thermostat exposes a separate demand sensor (e.g. Airzone
    ``binary_sensor.<zona>_demanda_de_suelo`` is ON only while the
    boiler loop is actually circulating, ignoring any A/C side).
    """
    if state is None or not state.state:
        return False
    if state.entity_id.startswith("binary_sensor."):
        return state.state.lower() in _BINARY_ON_STATES
    action: Any = state.attributes.get("hvac_action") if state.attributes else None
    if action:
        return str(action).lower() in _HEATING_ACTIONS
    return state.state.lower() in _HEATING_STATES


def _accumulate_hours(
    bucket: dict[datetime, float],
    span_start: datetime,
    span_end: datetime,
) -> None:
    """Spread a [span_start, span_end) heat-on span across hourly buckets.

    Each bucket key is the wall-clock hour-start in the local tz; the
    value is the fraction of that hour that was heat-on. A 30-minute
    span at 14:15 lands as ``hour_14: 0.5``. A span crossing 14:50 →
    15:20 lands as ``hour_14: 0.166, hour_15: 0.333``.
    """
    cur = span_start
    while cur < span_end:
        hour_start = cur.replace(minute=0, second=0, microsecond=0)
        next_hour = hour_start + timedelta(hours=1)
        chunk_end = min(next_hour, span_end)
        seconds = (chunk_end - cur).total_seconds()
        if seconds > 0:
            bucket[hour_start] = bucket.get(hour_start, 0.0) + seconds / 3600.0
        cur = chunk_end


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN check
        return None
    return out
