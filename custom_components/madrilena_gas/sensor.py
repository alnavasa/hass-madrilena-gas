"""Sensor platform for Madrileña Red de Gas.

Each entry materialises one device per meter with the following
sensors (cost block opt-in):

* ``meter_reading_m3`` — last bimestral lectura, monotonic, m³.
* ``last_reading_date`` — when that reading was taken.
* ``last_reading_type`` — Real / Estimada / Revisada / Facilitada.
* ``last_period_total_m3`` / ``_acs_m3`` / ``_heating_m3`` — last
  complete bimestral period split.
* ``acs_baseline_m3_persona_dia`` — derived ACS rate, diagnostic.
* ``last_ingest_at`` — when the bookmarklet last POSTed.
* ``data_age_days`` — days since last reading; nudges the user to
  click the bookmarklet again.

The Energy panel doesn't read these sensors; it reads the long-term
external statistics streams pushed by ``statistics_push.py`` (one each
for total / ACS / heating m³). The sensors are for the device card,
templates, and automations.

A brand-new entry (no POST yet) creates only the ``last_ingest_at``
diagnostic sensor; the rest appear after the first ingest reload.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_NAME,
    DEFAULT_NAME,
    DOMAIN,
)
from .coordinator import CoordinatorData, MadrilenaGasCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Materialise sensors once we know the meter id (i.e. after first POST).

    On a brand-new entry the store is empty and we don't yet know the
    meter id. Only the diagnostic ``last_ingest_at`` makes sense in that
    state — but even it relies on a meter-id-bound device, so for v0.1
    we cleanly exit and let the ingest endpoint reload us.
    """
    cache = hass.data[DOMAIN][entry.entry_id]
    coordinator: MadrilenaGasCoordinator = cache["coordinator"]
    install_name = (entry.data.get(CONF_NAME) or entry.title or DEFAULT_NAME).strip()

    meter_id = coordinator.meter_id
    if not meter_id:
        _LOGGER.info(
            "[%s] No meter bound yet — sensors will be created after the "
            "first bookmarklet POST triggers a config-entry reload.",
            entry.entry_id,
        )
        return

    entities: list[SensorEntity] = [
        MeterReadingSensor(coordinator, entry, install_name, meter_id),
        LastReadingDateSensor(coordinator, entry, install_name, meter_id),
        LastReadingTypeSensor(coordinator, entry, install_name, meter_id),
        LastPeriodTotalSensor(coordinator, entry, install_name, meter_id),
        LastPeriodAcsSensor(coordinator, entry, install_name, meter_id),
        LastPeriodHeatingSensor(coordinator, entry, install_name, meter_id),
        AcsBaselineSensor(coordinator, entry, install_name, meter_id),
        LastIngestAtSensor(coordinator, entry, install_name, meter_id),
        DataAgeDaysSensor(coordinator, entry, install_name, meter_id),
    ]
    async_add_entities(entities)


# ----------------------------------------------------------------------
# Base
# ----------------------------------------------------------------------


class _MeterSensor(CoordinatorEntity[MadrilenaGasCoordinator], SensorEntity):
    """Common scaffolding tying the entity to one meter device.

    has_entity_name=True lets HA compose the friendly name as
    ``<install-name> <entity-name>`` so the user sees e.g. "Casa
    principal Lectura del contador" without us splicing strings.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._install_name = install_name
        self._meter_id = meter_id
        # Stable identifier for the lifetime of this meter — the user
        # can rename the install without losing history.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{meter_id}")},
            name=install_name,
            manufacturer="Madrileña Red de Gas",
            model=f"Contador {meter_id}",
            configuration_url="https://ov.madrilena.es/consumos",
        )

    @property
    def _data(self) -> CoordinatorData | None:
        return self.coordinator.data


# ----------------------------------------------------------------------
# Meter / reading sensors
# ----------------------------------------------------------------------


class MeterReadingSensor(_MeterSensor, RestoreSensor):
    """Latest cumulative reading from the dial, in m³.

    TOTAL_INCREASING is the right state class even though the value
    only updates every ~60 days — HA happily handles infrequent
    updates and the Energy panel works off the external statistics
    anyway. RestoreSensor keeps the last value across HA restarts so
    the entity card never shows ``unknown`` mid-restart.
    """

    _attr_device_class = SensorDeviceClass.GAS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_icon = "mdi:counter"
    _attr_translation_key = "meter_reading"
    _attr_suggested_display_precision = 0  # Madrileña reports integer m³

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, meter_id)
        self._attr_name = "Lectura del contador"
        self._attr_unique_id = f"madrilena_gas_{meter_id}_meter_reading"
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)
            except (TypeError, ValueError):
                self._restored_value = None

    @property
    def native_value(self) -> float | None:
        data = self._data
        if not data or not data.readings:
            return self._restored_value
        latest = data.readings[0].lectura_m3
        if self._restored_value is not None and latest < self._restored_value - 0.5:
            _LOGGER.warning(
                "Meter reading dropped (%.1f → %.1f m³); keeping previous value",
                self._restored_value, latest,
            )
            return self._restored_value
        self._restored_value = latest
        return latest

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._data
        if not data or not data.readings:
            return {}
        latest = data.readings[0]
        return {
            "fecha": latest.fecha.isoformat(),
            "tipo": latest.tipo.value,
            "readings_count": len(data.readings),
            "meter_id": self._meter_id,
        }


class LastReadingDateSensor(_MeterSensor):
    """Date of the most recent reading. Timestamp device class.

    Anchored at midnight Madrid for the reading's calendar date — the
    portal doesn't expose a finer granularity.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"
    _attr_translation_key = "last_reading_date"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, meter_id)
        self._attr_name = "Última lectura"
        self._attr_unique_id = f"madrilena_gas_{meter_id}_last_reading_date"

    @property
    def native_value(self) -> datetime | None:
        data = self._data
        if not data or not data.readings:
            return None
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone) or dt_util.UTC
        return datetime.combine(
            data.readings[0].fecha,
            datetime.min.time(),
            tzinfo=local_tz,
        )


class LastReadingTypeSensor(_MeterSensor):
    """Reading type: Real / Estimada / Revisada / Facilitada / Unknown.

    Useful for templates: a user can flag "warn me when an estimada
    reading replaces a real one" via an automation.
    """

    _attr_icon = "mdi:tag-text"
    _attr_translation_key = "last_reading_type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, meter_id)
        self._attr_name = "Tipo última lectura"
        self._attr_unique_id = f"madrilena_gas_{meter_id}_last_reading_type"

    @property
    def native_value(self) -> str | None:
        data = self._data
        if not data or not data.readings:
            return None
        return data.readings[0].tipo.value


# ----------------------------------------------------------------------
# Period sensors — last complete bimestral period
# ----------------------------------------------------------------------


class _LastPeriodMixin:
    """Hook for the three last-period sensors to share period lookup."""

    coordinator: MadrilenaGasCoordinator

    def _last_dist(self):
        data = self.coordinator.data
        return data.last_distribution if data else None

    def _period_attrs(self) -> dict[str, Any]:
        dist = self._last_dist()
        if not dist:
            return {}
        period = dist.period
        return {
            "period_start": period.start.isoformat(),
            "period_end": period.end.isoformat(),
            "period_days": period.days,
            "fallback_uniform": dist.fallback_uniform,
        }


class LastPeriodTotalSensor(_MeterSensor, _LastPeriodMixin):
    """Total m³ consumed in the last complete bimestral period.

    State class TOTAL (not TOTAL_INCREASING) — the value resets each
    time a new period closes, so it isn't strictly monotonic.
    """

    _attr_device_class = SensorDeviceClass.GAS
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_icon = "mdi:fire"
    _attr_translation_key = "last_period_total"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, meter_id)
        self._attr_name = "Último periodo total"
        self._attr_unique_id = f"madrilena_gas_{meter_id}_last_period_total"

    @property
    def native_value(self) -> float | None:
        dist = self._last_dist()
        return dist.period.consumption_m3 if dist else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._period_attrs()


class LastPeriodAcsSensor(_MeterSensor, _LastPeriodMixin):
    """ACS portion of the last bimestral period."""

    _attr_device_class = SensorDeviceClass.GAS
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_icon = "mdi:water-thermometer"
    _attr_translation_key = "last_period_acs"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, meter_id)
        self._attr_name = "Último periodo ACS"
        self._attr_unique_id = f"madrilena_gas_{meter_id}_last_period_acs"

    @property
    def native_value(self) -> float | None:
        dist = self._last_dist()
        return dist.acs_total_m3 if dist else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._period_attrs()


class LastPeriodHeatingSensor(_MeterSensor, _LastPeriodMixin):
    """Heating portion of the last bimestral period."""

    _attr_device_class = SensorDeviceClass.GAS
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_icon = "mdi:radiator"
    _attr_translation_key = "last_period_heating"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, meter_id)
        self._attr_name = "Último periodo calefacción"
        self._attr_unique_id = f"madrilena_gas_{meter_id}_last_period_heating"

    @property
    def native_value(self) -> float | None:
        dist = self._last_dist()
        return dist.heating_total_m3 if dist else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = self._period_attrs()
        data = self._data
        if data:
            attrs["weather_coverage"] = round(data.weather_coverage, 3)
            attrs["climate_coverage"] = round(data.climate_coverage, 3)
        return attrs


# ----------------------------------------------------------------------
# Diagnostic
# ----------------------------------------------------------------------


class AcsBaselineSensor(_MeterSensor):
    """Per-person, per-day ACS m³ figure currently in use."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "m³/persona·día"
    _attr_icon = "mdi:water-percent"
    _attr_translation_key = "acs_baseline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 4

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, meter_id)
        self._attr_name = "Baseline ACS"
        self._attr_unique_id = f"madrilena_gas_{meter_id}_acs_baseline"

    @property
    def native_value(self) -> float | None:
        data = self._data
        return data.baseline.m3_per_person_day if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._data
        if not data:
            return {}
        return {
            "source": data.baseline.source,
            "summer_periods_used": data.baseline.summer_periods_used,
        }


class LastIngestAtSensor(_MeterSensor):
    """When the bookmarklet last POSTed successfully."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:download-circle"
    _attr_translation_key = "last_ingest_at"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, meter_id)
        self._attr_name = "Última actualización"
        self._attr_unique_id = f"madrilena_gas_{meter_id}_last_ingest_at"

    @property
    def native_value(self) -> datetime | None:
        data = self._data
        if not data or data.last_ingest_at is None:
            return None
        ts = data.last_ingest_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts


class DataAgeDaysSensor(_MeterSensor):
    """Days since the most recent reading.

    Crosses 60 → time to click the bookmarklet again. The user can wire
    a Notify automation off this. Recomputes on every coordinator tick
    so it advances at midnight without an ingest.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "d"
    _attr_icon = "mdi:clock-alert"
    _attr_translation_key = "data_age_days"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: MadrilenaGasCoordinator,
        entry: ConfigEntry,
        install_name: str,
        meter_id: str,
    ) -> None:
        super().__init__(coordinator, entry, install_name, meter_id)
        self._attr_name = "Días desde última lectura"
        self._attr_unique_id = f"madrilena_gas_{meter_id}_data_age_days"

    @property
    def native_value(self) -> int | None:
        data = self._data
        if not data or not data.readings:
            return None
        latest = data.readings[0].fecha
        today = dt_util.now().date()
        return max(0, (today - latest).days)
