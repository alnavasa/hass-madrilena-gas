"""DataUpdateCoordinator for Madrileña Red de Gas.

The coordinator does no I/O against the Madrileña portal — readings
arrive via the bookmarklet POST endpoint and live in a per-entry
:class:`ReadingStore`. The coordinator's job on each tick is:

1. **Read** current readings from the store (cheap, in-memory).
2. **Compute** derived state: periods, ACS baseline, daily distribution.
3. **Pull** outdoor temperature (HA recorder + Open-Meteo backfill) and
   climate-on hours (HA recorder).
4. **Push** the resulting cumulative streams to long-term statistics so
   the Energy panel renders one bar per civil day.
5. **Return** a snapshot dataclass that sensors read directly — no
   sensor needs to know about distribution algorithms.

The expensive bits (recorder reads, distribution loop, statistics
upsert) are bounded: ~13 periods × 67 days = ~870 days of work even on
a 2-year history. We cache the last computed snapshot keyed by the hash
of (readings, options), so a tick where nothing changed returns instantly.

The ingest endpoint calls ``async_request_refresh()`` after each
successful POST so fresh data flows to sensors immediately. The slow
periodic tick (1 h) catches midnight rollovers, options changes, and
late-arriving recorder data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .acs import AcsBaseline, derive_acs_baseline
from .const import (
    CONF_ACS_M3_PER_PERSON_DAY,
    CONF_ALQUILER_EUR_MES,
    CONF_CLIMATE_AREAS_M2,
    CONF_CLIMATE_ENTITIES,
    CONF_COST_MODE,
    CONF_DESCUENTO_PCT,
    CONF_ENABLE_COST,
    CONF_HDD_BASE_C,
    CONF_IEH_EUR_KWH,
    CONF_IVA_PCT,
    CONF_KWH_PER_M3,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PEOPLE,
    CONF_PRICE_EUR_KWH,
    CONF_TERM_FIJO_EUR_DIA,
    COST_MODE_ADVANCED,
    COST_MODE_SIMPLE,
    DEFAULT_DESCUENTO_PCT,
    DEFAULT_HDD_BASE_C,
    DEFAULT_IEH_EUR_KWH,
    DEFAULT_IVA_PCT,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .distribution import build_periods, distribute_period
from .models import (
    ClimateActivityHour,
    DailyWeather,
    DistributionResult,
    Period,
    Reading,
)
from .recorder_helpers import (
    fetch_climate_hours_from_recorder,
    fetch_daily_temps_from_recorder,
)
from .statistics_push import push_distribution_streams
from .store import ReadingStore
from .weather_history import fetch_daily_mean_temps

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CoordinatorData:
    """Snapshot the coordinator hands to sensors and helpers.

    All derived values live here so sensors stay dumb (just read fields).
    """

    readings: list[Reading]
    meter_id: str | None
    periods: list[Period]
    baseline: AcsBaseline
    distributions: list[DistributionResult]
    last_ingest_at: datetime | None
    weather_coverage: float = 0.0  # fraction of period-days with real temperature data
    climate_coverage: float = 0.0  # fraction of period-hours with climate data

    daily_total_m3_by_day: dict = field(default_factory=dict)
    daily_acs_m3_by_day: dict = field(default_factory=dict)
    daily_heating_m3_by_day: dict = field(default_factory=dict)

    @property
    def last_complete_period(self) -> Period | None:
        return self.periods[-1] if self.periods else None

    @property
    def last_distribution(self) -> DistributionResult | None:
        return self.distributions[-1] if self.distributions else None


class MadrilenaGasCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Thin orchestrator over store + distribution + statistics push."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: ReadingStore,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry
        self.store = store
        self._last_signature: tuple | None = None
        self._last_data: CoordinatorData | None = None

    @property
    def meter_id(self) -> str | None:
        return self.store.meter_id

    @property
    def options(self) -> dict:
        """Effective options: OptionsFlow on top of the original wizard data."""
        merged = dict(self.entry.data)
        merged.update(self.entry.options or {})
        return merged

    async def _async_update_data(self) -> CoordinatorData:
        readings = self.store.readings
        opts = self.options

        signature = self._compute_signature(readings, opts)
        if signature == self._last_signature and self._last_data is not None:
            # Nothing user-visible changed since last tick; reuse the
            # snapshot to avoid hammering the recorder for no reason.
            return self._last_data

        people = int(opts.get(CONF_PEOPLE, 0) or 0)
        manual_acs = opts.get(CONF_ACS_M3_PER_PERSON_DAY)
        hdd_base = float(opts.get(CONF_HDD_BASE_C, DEFAULT_HDD_BASE_C))
        outdoor_entity = (opts.get(CONF_OUTDOOR_TEMP_ENTITY) or "").strip() or None
        climate_entities = list(opts.get(CONF_CLIMATE_ENTITIES) or [])
        climate_areas = dict(opts.get(CONF_CLIMATE_AREAS_M2) or {})

        periods = build_periods(readings) if len(readings) >= 2 else []
        baseline = derive_acs_baseline(
            periods,
            people=people,
            manual_override=float(manual_acs) if manual_acs else None,
        )

        distributions: list[DistributionResult] = []
        weather_days_total = 0
        weather_days_known = 0
        climate_hours_total = 0
        climate_hours_known = 0

        for period in periods:
            weather, w_known = await self._weather_for_period(period, outdoor_entity)
            weather_days_total += period.days
            weather_days_known += w_known

            climate_hours, c_known = await self._climate_hours_for_period(
                period, climate_entities, areas_m2=climate_areas,
            )
            climate_hours_total += period.days * 24
            climate_hours_known += c_known

            distributions.append(
                distribute_period(
                    period,
                    weather=weather,
                    climate_hours=climate_hours,
                    acs_baseline=baseline,
                    people=people,
                    hdd_base_c=hdd_base,
                )
            )

        weather_coverage = (
            weather_days_known / weather_days_total if weather_days_total else 0.0
        )
        climate_coverage = (
            climate_hours_known / climate_hours_total if climate_hours_total else 0.0
        )

        # Flatten daily shares for convenient sensor lookups.
        daily_total: dict = {}
        daily_acs: dict = {}
        daily_heating: dict = {}
        for dist in distributions:
            for ds in dist.daily:
                daily_total[ds.day] = daily_total.get(ds.day, 0.0) + ds.total_m3
                daily_acs[ds.day] = daily_acs.get(ds.day, 0.0) + ds.acs_m3
                daily_heating[ds.day] = daily_heating.get(ds.day, 0.0) + ds.heating_m3

        snapshot = CoordinatorData(
            readings=readings,
            meter_id=self.store.meter_id,
            periods=periods,
            baseline=baseline,
            distributions=distributions,
            last_ingest_at=self.store.last_ingest_at,
            weather_coverage=weather_coverage,
            climate_coverage=climate_coverage,
            daily_total_m3_by_day=daily_total,
            daily_acs_m3_by_day=daily_acs,
            daily_heating_m3_by_day=daily_heating,
        )

        # Push long-term statistics on every refresh. The recorder
        # upserts by (statistic_id, start) so re-pushing the full
        # history is cheap and idempotent (Canal does the same).
        if distributions and self.store.meter_id:
            cost_per_m3, cost_per_day = self._cost_coefficients(opts)
            try:
                await push_distribution_streams(
                    self.hass,
                    meter_id=self.store.meter_id,
                    install_name=self.entry.title,
                    distributions=distributions,
                    cost_per_m3=cost_per_m3,
                    cost_per_day=cost_per_day,
                )
            except Exception:
                _LOGGER.exception("Statistics push failed; sensor data still valid")

        self._last_signature = signature
        self._last_data = snapshot
        return snapshot

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cost_coefficients(opts: dict) -> tuple[float | None, float]:
        """Resolve the (€/m³, €/día) pair for the EUR statistic stream.

        Returns ``(None, 0.0)`` when cost tracking is off or essential
        inputs are missing — the push layer reads ``None`` as "skip".

        * **Simple mode** — ``price_eur_kwh`` is the all-in €/kWh; multiply
          by the PCS factor and stop. No fixed term, no IVA arithmetic.
          Cost ≈ proportional to consumption.
        * **Advanced mode** — apply the Spanish gas-bill formula:

              cost_per_m3 = (price + IEH) × kwh/m³ × (1 - desc/100) × (1 + IVA/100)
              cost_per_day = (fijo + alquiler/30) × (1 + IVA/100)

          Discount applies only to the variable term (mirrors how Endesa
          prints "Descuento promocional -X % x <variable>" on the
          invoice). IVA applies to the whole subtotal — the reduced 10 %
          gas rate by default, override-able if the law changes.
        """
        if not opts.get(CONF_ENABLE_COST):
            return None, 0.0

        kwh_per_m3 = float(opts.get(CONF_KWH_PER_M3) or 0)
        price_eur_kwh = float(opts.get(CONF_PRICE_EUR_KWH) or 0)
        if kwh_per_m3 <= 0 or price_eur_kwh <= 0:
            return None, 0.0

        mode = opts.get(CONF_COST_MODE) or COST_MODE_SIMPLE
        if mode != COST_MODE_ADVANCED:
            return price_eur_kwh * kwh_per_m3, 0.0

        ieh = float(opts.get(CONF_IEH_EUR_KWH) or DEFAULT_IEH_EUR_KWH)
        fijo_dia = float(opts.get(CONF_TERM_FIJO_EUR_DIA) or 0)
        alquiler_mes = float(opts.get(CONF_ALQUILER_EUR_MES) or 0)
        iva_pct = float(opts.get(CONF_IVA_PCT) or DEFAULT_IVA_PCT)
        desc_pct = float(opts.get(CONF_DESCUENTO_PCT) or DEFAULT_DESCUENTO_PCT)

        iva_mult = 1.0 + iva_pct / 100.0
        discount_mult = 1.0 - desc_pct / 100.0

        variable_per_m3 = price_eur_kwh * kwh_per_m3 * discount_mult
        ieh_per_m3 = ieh * kwh_per_m3
        cost_per_m3 = (variable_per_m3 + ieh_per_m3) * iva_mult
        cost_per_day = (fijo_dia + alquiler_mes / 30.0) * iva_mult
        return cost_per_m3, cost_per_day

    def _compute_signature(self, readings: list[Reading], opts: dict) -> tuple:
        """A cheap-to-hash key that changes whenever the snapshot would.

        Includes the latest reading and the OptionsFlow-relevant fields.
        Excludes ``last_ingest_at`` so a re-POST with identical readings
        still counts as a no-op.
        """
        last = readings[0] if readings else None
        return (
            len(readings),
            (last.fecha.isoformat(), last.lectura_m3) if last else None,
            opts.get(CONF_PEOPLE),
            opts.get(CONF_ACS_M3_PER_PERSON_DAY),
            opts.get(CONF_HDD_BASE_C),
            opts.get(CONF_OUTDOOR_TEMP_ENTITY),
            tuple(sorted(opts.get(CONF_CLIMATE_ENTITIES) or [])),
            tuple(sorted((opts.get(CONF_CLIMATE_AREAS_M2) or {}).items())),
            opts.get(CONF_ENABLE_COST),
            opts.get(CONF_COST_MODE),
            opts.get(CONF_KWH_PER_M3),
            opts.get(CONF_PRICE_EUR_KWH),
            opts.get(CONF_TERM_FIJO_EUR_DIA),
            opts.get(CONF_ALQUILER_EUR_MES),
            opts.get(CONF_IEH_EUR_KWH),
            opts.get(CONF_IVA_PCT),
            opts.get(CONF_DESCUENTO_PCT),
        )

    async def _weather_for_period(
        self,
        period: Period,
        outdoor_entity: str | None,
    ) -> tuple[list[DailyWeather], int]:
        """Pull daily mean temps for a period; return (data, days_known).

        Strategy:
          1. Ask HA recorder for the configured outdoor temp entity.
          2. For days the recorder doesn't have, fall back to Open-Meteo
             Archive (free ERA5 reanalysis, no API key).
          3. If neither yields anything, return an empty list — the
             distribution falls back to uniform.
        """
        from_recorder: list[DailyWeather] = []
        if outdoor_entity:
            try:
                from_recorder = await fetch_daily_temps_from_recorder(
                    self.hass,
                    outdoor_entity,
                    period.start,
                    period.end,
                )
            except Exception:
                _LOGGER.exception(
                    "Recorder weather read failed for %s in %s..%s",
                    outdoor_entity, period.start, period.end,
                )

        recorder_days = {w.day for w in from_recorder}
        period_days = {
            period.start + (period.end - period.start) * 0  # placeholder
            for _ in range(0)
        }
        # Build the list of period days; reuse distribution._days_in_period
        from .distribution import _days_in_period
        all_days = set(_days_in_period(period))
        missing = sorted(all_days - recorder_days)

        from_archive: list[DailyWeather] = []
        if missing:
            lat = self.hass.config.latitude
            lon = self.hass.config.longitude
            if lat is not None and lon is not None:
                from_archive = await fetch_daily_mean_temps(
                    float(lat), float(lon), missing[0], missing[-1],
                )
                # Keep only days we actually missed (the API may return
                # extras at the edges if start/end aren't aligned).
                from_archive = [w for w in from_archive if w.day in all_days]

        merged_by_day: dict = {w.day: w for w in from_archive}
        for w in from_recorder:
            merged_by_day[w.day] = w  # recorder wins for overlapping days
        merged = sorted(merged_by_day.values(), key=lambda w: w.day)
        return merged, len(merged)

    async def _climate_hours_for_period(
        self,
        period: Period,
        climate_entities: list[str],
        areas_m2: dict[str, float] | None = None,
    ) -> tuple[list[ClimateActivityHour], int]:
        if not climate_entities:
            return [], 0
        try:
            hours = await fetch_climate_hours_from_recorder(
                self.hass,
                climate_entities,
                period.start,
                period.end,
                areas_m2=areas_m2,
            )
        except Exception:
            _LOGGER.exception(
                "Recorder climate read failed for %s in %s..%s",
                climate_entities, period.start, period.end,
            )
            return [], 0
        # "Known" = hours where we have any signal, even zero. The
        # recorder helper returns one entry per hour it observed.
        return hours, len(hours)


def utcnow() -> datetime:
    """Wrap so tests can monkeypatch."""
    return datetime.now(UTC)
