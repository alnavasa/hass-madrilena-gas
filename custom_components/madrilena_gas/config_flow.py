"""Config flow for Madrileña Red de Gas.

Two-step wizard:

1. **User step** — installation name, HA URL (where the bookmarklet
   POSTs), number of people, climate entities (multi-select, may be
   empty), outdoor temperature entity (optional), and an opt-in
   ``enable_cost`` toggle.

2. **Cost step** — only when ``enable_cost`` was ticked. Asks for the
   m³ → kWh PCS factor and the marginal €/kWh from the
   commercializadora (Endesa, Naturgy, etc.). Madrileña is just the
   distributor and doesn't bill the energy itself.

After install, every parameter is editable via an ``OptionsFlow`` so
the user can change the people count when a kid moves out, swap the
weather source, or pin the ACS baseline manually.

On submit of the user step we generate a 192-bit token
(``secrets.token_hex(24)``) for this entry. The flow manager allocates
the real ``entry_id`` after ``async_create_entry`` returns — **the
bookmarklet and its install notification cannot be built here**; they
are published from ``async_setup_entry`` (see ``__init__.py``), which
runs with the final ``entry.entry_id`` bound.

The entry starts with no bound meter id. The first successful POST via
the bookmarklet binds it (see ``ingest.py``) and triggers an entity
reload so the sensors materialise without an HA restart.

Re-auth (token rotation) is not modelled — if the user needs a new
token, they delete and recreate the entry.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ACS_M3_PER_PERSON_DAY,
    CONF_ALQUILER_EUR_MES,
    CONF_CLIMATE_AREAS_M2,
    CONF_CLIMATE_ENTITIES,
    CONF_COST_MODE,
    CONF_DESCUENTO_PCT,
    CONF_ENABLE_COST,
    CONF_HA_URL,
    CONF_HDD_BASE_C,
    CONF_IEH_EUR_KWH,
    CONF_IVA_PCT,
    CONF_KWH_PER_M3,
    CONF_NAME,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PEOPLE,
    CONF_PRICE_EUR_KWH,
    CONF_TERM_FIJO_EUR_DIA,
    CONF_TOKEN,
    COST_MODE_ADVANCED,
    COST_MODE_SIMPLE,
    DEFAULT_AREA_M2,
    DEFAULT_DESCUENTO_PCT,
    DEFAULT_HDD_BASE_C,
    DEFAULT_IEH_EUR_KWH,
    DEFAULT_IVA_PCT,
    DEFAULT_KWH_PER_M3,
    DEFAULT_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Realistic ranges for residential households.
_PEOPLE_MIN, _PEOPLE_MAX = 0, 20
_HDD_BASE_MIN, _HDD_BASE_MAX = 10.0, 22.0
_AREA_M2_MIN, _AREA_M2_MAX = 0.1, 500.0
_ACS_MIN, _ACS_MAX = 0.0, 0.5
_KWH_PER_M3_MIN, _KWH_PER_M3_MAX = 9.0, 13.0
_PRICE_MIN, _PRICE_MAX = 0.0, 1.0


def _people_field(default: int = 1) -> dict:
    return {
        vol.Required(CONF_PEOPLE, default=default): NumberSelector(
            NumberSelectorConfig(
                min=_PEOPLE_MIN,
                max=_PEOPLE_MAX,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
    }


def _climate_field(default: list[str] | None = None) -> dict:
    """Heating activity selector.

    Accepts ``climate.*`` (uses ``hvac_action == 'heating'`` or state in
    ``{heat, heat_cool, auto}``) and ``binary_sensor.*`` (state == 'on').
    The latter is the cleanest signal for setups like Airzone where the
    boiler-demand sensor is exposed separately from the thermostat —
    avoids counting the electric A/C side that may run alongside the
    floor-heating loop on big setpoint jumps.
    """
    return {
        vol.Optional(CONF_CLIMATE_ENTITIES, default=default or []): EntitySelector(
            EntitySelectorConfig(domain=["climate", "binary_sensor"], multiple=True)
        ),
    }


def _outdoor_temp_field(default: str = "") -> dict:
    """Selector accepts both ``weather.*`` (uses .attributes.temperature)
    and ``sensor.*`` (state is the temperature). Optional — empty falls
    back to Open-Meteo."""
    cfg = EntitySelectorConfig(domain=["sensor", "weather"], multiple=False)
    if default:
        return {vol.Optional(CONF_OUTDOOR_TEMP_ENTITY, default=default): EntitySelector(cfg)}
    return {vol.Optional(CONF_OUTDOOR_TEMP_ENTITY): EntitySelector(cfg)}


def _areas_schema(
    entity_ids: list[str], current: dict[str, float] | None = None,
) -> vol.Schema:
    """Build a schema with one m² field per selected entity.

    Voluptuous accepts dotted strings as keys (``climate.salon``); HA
    renders them as field labels in the UI. Default per zone is
    ``DEFAULT_AREA_M2`` (1.0 = pure zone-counting). Users with multi-zone
    setups (Airzone) override with realistic m² for accurate weighting.
    """
    current = current or {}
    fields: dict = {}
    for eid in entity_ids:
        default = float(current.get(eid, DEFAULT_AREA_M2))
        fields[vol.Required(eid, default=default)] = NumberSelector(
            NumberSelectorConfig(
                min=_AREA_M2_MIN,
                max=_AREA_M2_MAX,
                step=0.1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="m²",
            )
        )
    return vol.Schema(fields)


def _hdd_base_field(default: float = DEFAULT_HDD_BASE_C) -> dict:
    return {
        vol.Required(CONF_HDD_BASE_C, default=default): NumberSelector(
            NumberSelectorConfig(
                min=_HDD_BASE_MIN,
                max=_HDD_BASE_MAX,
                step=0.5,
                mode=NumberSelectorMode.BOX,
            )
        ),
    }


def _acs_override_field(default: float | None = None) -> dict:
    """ACS manual override (m³/persona/día). Optional — leave blank to
    auto-derive from summer periods.

    NumberSelector doesn't accept ``None`` as a default cleanly, so the
    field is omitted from the schema when there's no current value;
    blank input round-trips as 0, which the OptionsFlow treats as
    "unset" (revert to auto)."""
    base = NumberSelectorConfig(
        min=_ACS_MIN,
        max=_ACS_MAX,
        step="any",
        mode=NumberSelectorMode.BOX,
    )
    if default and default > 0:
        return {vol.Optional(CONF_ACS_M3_PER_PERSON_DAY, default=float(default)): NumberSelector(base)}
    return {vol.Optional(CONF_ACS_M3_PER_PERSON_DAY): NumberSelector(base)}


def _cost_fields(
    *,
    kwh_per_m3: float = DEFAULT_KWH_PER_M3,
    price: float = 0.07,
    mode: str = COST_MODE_SIMPLE,
) -> dict:
    """Mode picker + the two fields that exist in *both* modes.

    Same shape as v0.2.4 plus the new mode dropdown on top — keeps
    existing entries auto-migrating into the simple branch when the
    mode key is missing from ``entry.options``.
    """
    return {
        vol.Required(CONF_COST_MODE, default=mode): SelectSelector(
            SelectSelectorConfig(
                options=[
                    {"value": COST_MODE_SIMPLE, "label": "Modo sencillo"},
                    {"value": COST_MODE_ADVANCED, "label": "Modo avanzado"},
                ],
                mode=SelectSelectorMode.LIST,
                translation_key="cost_mode",
            )
        ),
        vol.Required(CONF_KWH_PER_M3, default=kwh_per_m3): NumberSelector(
            NumberSelectorConfig(
                min=_KWH_PER_M3_MIN,
                max=_KWH_PER_M3_MAX,
                step="any",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_PRICE_EUR_KWH, default=price): NumberSelector(
            NumberSelectorConfig(
                min=_PRICE_MIN,
                max=_PRICE_MAX,
                step="any",
                mode=NumberSelectorMode.BOX,
            )
        ),
    }


def _advanced_cost_fields(
    *,
    fijo_dia: float = 0.0,
    alquiler_mes: float = 0.0,
    ieh_kwh: float = DEFAULT_IEH_EUR_KWH,
    iva_pct: float = DEFAULT_IVA_PCT,
    desc_pct: float = DEFAULT_DESCUENTO_PCT,
) -> dict:
    """Five extra fields shown only in advanced mode.

    Defaults track 2026 Endesa "Tarifa One Gas" RL.2 + reduced 10 % IVA.
    User overrides if their commercializadora differs.
    """
    return {
        vol.Required(CONF_TERM_FIJO_EUR_DIA, default=fijo_dia): NumberSelector(
            NumberSelectorConfig(
                min=0.0, max=5.0, step="any", mode=NumberSelectorMode.BOX,
                unit_of_measurement="€/día",
            )
        ),
        vol.Required(CONF_ALQUILER_EUR_MES, default=alquiler_mes): NumberSelector(
            NumberSelectorConfig(
                min=0.0, max=20.0, step="any", mode=NumberSelectorMode.BOX,
                unit_of_measurement="€/mes",
            )
        ),
        vol.Required(CONF_IEH_EUR_KWH, default=ieh_kwh): NumberSelector(
            NumberSelectorConfig(
                min=0.0, max=0.05, step="any", mode=NumberSelectorMode.BOX,
                unit_of_measurement="€/kWh",
            )
        ),
        vol.Required(CONF_IVA_PCT, default=iva_pct): NumberSelector(
            NumberSelectorConfig(
                min=0.0, max=30.0, step=0.5, mode=NumberSelectorMode.BOX,
                unit_of_measurement="%",
            )
        ),
        vol.Required(CONF_DESCUENTO_PCT, default=desc_pct): NumberSelector(
            NumberSelectorConfig(
                min=0.0, max=100.0, step=1, mode=NumberSelectorMode.BOX,
                unit_of_measurement="%",
            )
        ),
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._name: str = DEFAULT_NAME
        self._ha_url: str = ""
        self._token: str = ""
        self._people: int = 1
        self._climate_entities: list[str] = []
        self._climate_areas: dict[str, float] = {}
        self._outdoor_entity: str = ""
        self._hdd_base: float = DEFAULT_HDD_BASE_C
        self._enable_cost: bool = False
        self._cost_params: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1 — name, URL, people, climates, outdoor temp, cost toggle."""
        errors: dict[str, str] = {}
        default_url = (self.hass.config.external_url or "").rstrip("/") or (
            self.hass.config.internal_url or ""
        ).rstrip("/")

        if user_input is not None:
            self._name = (user_input.get(CONF_NAME) or DEFAULT_NAME).strip() or DEFAULT_NAME
            self._ha_url = (user_input.get(CONF_HA_URL) or default_url or "").strip().rstrip("/")
            self._people = int(user_input.get(CONF_PEOPLE, 1) or 0)
            self._climate_entities = list(user_input.get(CONF_CLIMATE_ENTITIES) or [])
            self._outdoor_entity = (user_input.get(CONF_OUTDOOR_TEMP_ENTITY) or "").strip()
            self._hdd_base = float(user_input.get(CONF_HDD_BASE_C, DEFAULT_HDD_BASE_C))
            self._enable_cost = bool(user_input.get(CONF_ENABLE_COST, False))

            if not self._ha_url:
                errors["base"] = "missing_ha_url"
            elif not (self._ha_url.startswith("http://") or self._ha_url.startswith("https://")):
                errors[CONF_HA_URL] = "invalid_ha_url"
            else:
                self._token = secrets.token_hex(24)  # 48 chars, 192 bits
                if self._climate_entities:
                    return await self.async_step_areas()
                if self._enable_cost:
                    return await self.async_step_cost()
                return await self._create_entry()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self._name): str,
                vol.Required(CONF_HA_URL, default=default_url): str,
                **_people_field(self._people),
                **_climate_field(self._climate_entities),
                **_outdoor_temp_field(self._outdoor_entity),
                **_hdd_base_field(self._hdd_base),
                vol.Required(CONF_ENABLE_COST, default=self._enable_cost): bool,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "default_url": default_url or "(no detectada — pega la URL HTTPS de tu HA)",
            },
        )

    async def async_step_areas(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Step 2 — m² per selected climate / binary_sensor (only when any picked)."""
        if user_input is not None:
            self._climate_areas = {
                eid: float(user_input.get(eid, DEFAULT_AREA_M2) or DEFAULT_AREA_M2)
                for eid in self._climate_entities
            }
            if self._enable_cost:
                return await self.async_step_cost()
            return await self._create_entry()

        return self.async_show_form(
            step_id="areas",
            data_schema=_areas_schema(self._climate_entities, self._climate_areas),
            description_placeholders={"count": str(len(self._climate_entities))},
        )

    async def async_step_cost(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 3 — pick cost mode + the two universal fields.

        Simple mode finishes here (the two fields are everything it
        needs). Advanced mode passes through to ``async_step_cost_advanced``
        for the five extra fields (fijo, alquiler, IEH, IVA, descuento).
        """
        if user_input is not None:
            mode = user_input.get(CONF_COST_MODE) or COST_MODE_SIMPLE
            self._cost_params = {
                CONF_COST_MODE: mode,
                CONF_KWH_PER_M3: float(user_input[CONF_KWH_PER_M3]),
                CONF_PRICE_EUR_KWH: float(user_input[CONF_PRICE_EUR_KWH]),
            }
            if mode == COST_MODE_ADVANCED:
                return await self.async_step_cost_advanced()
            return await self._create_entry()

        return self.async_show_form(
            step_id="cost",
            data_schema=vol.Schema(_cost_fields()),
        )

    async def async_step_cost_advanced(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Step 3b — advanced-mode extras (fijo, alquiler, IEH, IVA, desc)."""
        if user_input is not None:
            self._cost_params.update(
                {
                    CONF_TERM_FIJO_EUR_DIA: float(user_input[CONF_TERM_FIJO_EUR_DIA]),
                    CONF_ALQUILER_EUR_MES: float(user_input[CONF_ALQUILER_EUR_MES]),
                    CONF_IEH_EUR_KWH: float(user_input[CONF_IEH_EUR_KWH]),
                    CONF_IVA_PCT: float(user_input[CONF_IVA_PCT]),
                    CONF_DESCUENTO_PCT: float(user_input[CONF_DESCUENTO_PCT]),
                }
            )
            return await self._create_entry()

        return self.async_show_form(
            step_id="cost_advanced",
            data_schema=vol.Schema(_advanced_cost_fields()),
        )

    async def _create_entry(self) -> FlowResult:
        data: dict[str, Any] = {
            CONF_NAME: self._name,
            CONF_TOKEN: self._token,
            CONF_HA_URL: self._ha_url,
            CONF_PEOPLE: self._people,
            CONF_CLIMATE_ENTITIES: self._climate_entities,
            CONF_CLIMATE_AREAS_M2: self._climate_areas,
            CONF_OUTDOOR_TEMP_ENTITY: self._outdoor_entity,
            CONF_HDD_BASE_C: self._hdd_base,
            CONF_ENABLE_COST: self._enable_cost,
            # Empty until the first successful POST sets it (see ingest.py).
            "meter_id": "",
        }
        if self._enable_cost:
            data.update(self._cost_params)
        return self.async_create_entry(title=self._name, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MadrilenaGasOptionsFlow:
        return MadrilenaGasOptionsFlow(config_entry)


class MadrilenaGasOptionsFlow(config_entries.OptionsFlow):
    """Edit every wizard parameter post-install + ACS override.

    Stored as ``entry.options`` (HA convention). On save, the entry
    reloads via the update listener wired in ``__init__.py`` and the
    new values flow through to coordinator + sensors.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._pending: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        merged = {**self._entry.data, **self._entry.options}

        if user_input is not None:
            self._pending = {
                CONF_PEOPLE: int(user_input.get(CONF_PEOPLE, 1) or 0),
                CONF_CLIMATE_ENTITIES: list(user_input.get(CONF_CLIMATE_ENTITIES) or []),
                CONF_OUTDOOR_TEMP_ENTITY: (user_input.get(CONF_OUTDOOR_TEMP_ENTITY) or "").strip(),
                CONF_HDD_BASE_C: float(user_input.get(CONF_HDD_BASE_C, DEFAULT_HDD_BASE_C)),
                CONF_ENABLE_COST: bool(user_input.get(CONF_ENABLE_COST, False)),
            }
            acs_val = user_input.get(CONF_ACS_M3_PER_PERSON_DAY)
            if acs_val and float(acs_val) > 0:
                self._pending[CONF_ACS_M3_PER_PERSON_DAY] = float(acs_val)
            if self._pending[CONF_ENABLE_COST]:
                self._pending[CONF_COST_MODE] = (
                    user_input.get(CONF_COST_MODE) or COST_MODE_SIMPLE
                )
                self._pending[CONF_KWH_PER_M3] = float(user_input.get(CONF_KWH_PER_M3, DEFAULT_KWH_PER_M3))
                self._pending[CONF_PRICE_EUR_KWH] = float(user_input.get(CONF_PRICE_EUR_KWH, 0.07))
            if self._pending[CONF_CLIMATE_ENTITIES]:
                return await self.async_step_areas()
            # No climates selected → drop any stale per-zone areas.
            self._pending[CONF_CLIMATE_AREAS_M2] = {}
            return await self._maybe_step_cost_advanced_then_finish()

        schema = vol.Schema(
            {
                **_people_field(int(merged.get(CONF_PEOPLE, 1) or 1)),
                **_climate_field(list(merged.get(CONF_CLIMATE_ENTITIES) or [])),
                **_outdoor_temp_field(merged.get(CONF_OUTDOOR_TEMP_ENTITY) or ""),
                **_hdd_base_field(float(merged.get(CONF_HDD_BASE_C, DEFAULT_HDD_BASE_C))),
                **_acs_override_field(merged.get(CONF_ACS_M3_PER_PERSON_DAY)),
                vol.Required(
                    CONF_ENABLE_COST,
                    default=bool(merged.get(CONF_ENABLE_COST, False)),
                ): bool,
                **_cost_fields(
                    kwh_per_m3=float(merged.get(CONF_KWH_PER_M3, DEFAULT_KWH_PER_M3)),
                    price=float(merged.get(CONF_PRICE_EUR_KWH, 0.07)),
                    mode=str(merged.get(CONF_COST_MODE) or COST_MODE_SIMPLE),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_areas(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Per-zone m² edit step (shown when at least one entity is picked)."""
        merged = {**self._entry.data, **self._entry.options}
        current_areas = dict(merged.get(CONF_CLIMATE_AREAS_M2) or {})
        entities = self._pending.get(CONF_CLIMATE_ENTITIES) or []

        if user_input is not None:
            self._pending[CONF_CLIMATE_AREAS_M2] = {
                eid: float(user_input.get(eid, DEFAULT_AREA_M2) or DEFAULT_AREA_M2)
                for eid in entities
            }
            return await self._maybe_step_cost_advanced_then_finish()

        return self.async_show_form(
            step_id="areas",
            data_schema=_areas_schema(entities, current_areas),
            description_placeholders={"count": str(len(entities))},
        )

    async def async_step_cost_advanced(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Advanced-mode extras editable post-install (mirrors ConfigFlow)."""
        merged = {**self._entry.data, **self._entry.options}

        if user_input is not None:
            self._pending[CONF_TERM_FIJO_EUR_DIA] = float(user_input[CONF_TERM_FIJO_EUR_DIA])
            self._pending[CONF_ALQUILER_EUR_MES] = float(user_input[CONF_ALQUILER_EUR_MES])
            self._pending[CONF_IEH_EUR_KWH] = float(user_input[CONF_IEH_EUR_KWH])
            self._pending[CONF_IVA_PCT] = float(user_input[CONF_IVA_PCT])
            self._pending[CONF_DESCUENTO_PCT] = float(user_input[CONF_DESCUENTO_PCT])
            return self.async_create_entry(title="", data=self._pending)

        return self.async_show_form(
            step_id="cost_advanced",
            data_schema=vol.Schema(
                _advanced_cost_fields(
                    fijo_dia=float(merged.get(CONF_TERM_FIJO_EUR_DIA) or 0.0),
                    alquiler_mes=float(merged.get(CONF_ALQUILER_EUR_MES) or 0.0),
                    ieh_kwh=float(merged.get(CONF_IEH_EUR_KWH) or DEFAULT_IEH_EUR_KWH),
                    iva_pct=float(merged.get(CONF_IVA_PCT) or DEFAULT_IVA_PCT),
                    desc_pct=float(merged.get(CONF_DESCUENTO_PCT) or DEFAULT_DESCUENTO_PCT),
                )
            ),
        )

    async def _maybe_step_cost_advanced_then_finish(self) -> FlowResult:
        """Route to the advanced-cost step when needed; else finish."""
        if (
            self._pending.get(CONF_ENABLE_COST)
            and self._pending.get(CONF_COST_MODE) == COST_MODE_ADVANCED
        ):
            return await self.async_step_cost_advanced()
        return self.async_create_entry(title="", data=self._pending)
