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
)

from .const import (
    CONF_ACS_M3_PER_PERSON_DAY,
    CONF_AUTOPILOT_ENABLED,
    CONF_CLIMATE_AREAS_M2,
    CONF_CLIMATE_ENTITIES,
    CONF_DNI,
    CONF_ENABLE_COST,
    CONF_HA_URL,
    CONF_HDD_BASE_C,
    CONF_KWH_PER_M3,
    CONF_NAME,
    CONF_OTP,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PASSWORD,
    CONF_PEOPLE,
    CONF_PRICE_EUR_KWH,
    CONF_TOKEN,
    DEFAULT_AREA_M2,
    DEFAULT_HDD_BASE_C,
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
) -> dict:
    return {
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
        # Reauth state — populated when ``async_step_reauth`` is the
        # entry-point. Carries the entry being reauth'd, the typed
        # credentials, and the opaque login context that bridges
        # ``begin_login`` → ``submit_otp``.
        self._madrilena_reauth_entry_id: str | None = None
        self._reauth_dni: str = ""
        self._reauth_password: str = ""
        self._login_ctx: Any = None

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
        """Step 3 — cost parameters (only when enable_cost was ticked)."""
        if user_input is not None:
            self._cost_params = {
                CONF_KWH_PER_M3: float(user_input[CONF_KWH_PER_M3]),
                CONF_PRICE_EUR_KWH: float(user_input[CONF_PRICE_EUR_KWH]),
            }
            return await self._create_entry()

        return self.async_show_form(
            step_id="cost",
            data_schema=vol.Schema(_cost_fields()),
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Reauth entry-point.

        Two cases:

        * **Bookmarklet-only entry** (autopilot disabled). Nothing to
          re-auth — the bookmarklet uses the user's browser cookies.
          We abort with a friendly message telling them to recreate the
          entry if they really need a new HA-side token.
        * **Autopilot entry** (autopilot enabled, session expired or
          credentials rejected). Hand off to :func:`async_step_reauth_confirm`
          so the user re-enters their portal credentials and the OTP.
        """
        # Discover the entry being reauth'd from the flow context (HA
        # populates it with the entry_id when ``entry.async_start_reauth``
        # is called).
        entry_id = self.context.get("entry_id")
        if entry_id:
            existing = self.hass.config_entries.async_get_entry(entry_id)
            if existing and existing.options.get(CONF_AUTOPILOT_ENABLED):
                self._madrilena_reauth_entry_id = entry_id
                return await self.async_step_reauth_confirm()
        return self.async_abort(reason="reauth_not_supported")

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Confirm + trigger ``begin_login`` (the portal will email an OTP).

        We split this from the OTP step so the email is only sent at
        the moment the user is ready to read it — the OTP TTL is
        short, so pre-fetching it would risk having it expire before
        they type it.
        """
        from .madrilena_client import (  # local import — keeps optional dep behaviour
            MadrilenaClient,
            MadrilenaClientError,
            MadrilenaClientNotImplemented,
        )
        from .secrets_store import CredentialsStore

        if not self._madrilena_reauth_entry_id:
            return self.async_abort(reason="reauth_not_supported")
        creds = CredentialsStore(self.hass, self._madrilena_reauth_entry_id)
        await creds.async_load()

        if user_input is not None:
            dni = (user_input.get(CONF_DNI) or creds.dni or "").strip()
            password = (user_input.get(CONF_PASSWORD) or creds.password or "").strip()
            if not dni or not password:
                return self.async_show_form(
                    step_id="reauth_confirm",
                    data_schema=self._reauth_confirm_schema(creds.dni),
                    errors={"base": "missing_credentials"},
                )

            import aiohttp

            from .secrets_store import SessionStore

            errors: dict[str, str] = {}
            try:
                async with aiohttp.ClientSession() as http:
                    client = MadrilenaClient(http)
                    self._login_ctx = await client.begin_login(dni, password)
                    # If the portal trusts this device we already have
                    # a usable session — persist it and skip the OTP
                    # step entirely.
                    if not self._login_ctx.needs_otp:
                        payload = self._login_ctx.session_payload
                        await creds.async_save(dni=dni, password=password)
                        session_store = SessionStore(
                            self.hass, self._madrilena_reauth_entry_id,
                        )
                        await session_store.async_save_payload(payload.to_dict())
                        entry = self.hass.config_entries.async_get_entry(
                            self._madrilena_reauth_entry_id,
                        )
                        if entry is None:
                            return self.async_abort(reason="reauth_not_supported")
                        return self.async_update_reload_and_abort(
                            entry, reason="reauth_successful",
                        )
                # MFA required — keep the credentials around and route
                # to the OTP step.
                await creds.async_save(dni=dni, password=password)
                self._reauth_dni = dni
                self._reauth_password = password
                return await self.async_step_otp()
            except MadrilenaClientNotImplemented:
                errors["base"] = "client_not_implemented"
            except MadrilenaClientError:
                _LOGGER.exception("Reauth begin_login failed")
                errors["base"] = "login_failed"

            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=self._reauth_confirm_schema(dni),
                errors=errors,
            )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._reauth_confirm_schema(creds.dni),
        )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Second reauth step: user types the OTP they just received."""
        from .madrilena_client import (
            InvalidOtp,
            MadrilenaClient,
            MadrilenaClientError,
            MadrilenaClientNotImplemented,
        )
        from .secrets_store import SessionStore

        if not self._madrilena_reauth_entry_id or self._login_ctx is None:
            return self.async_abort(reason="reauth_not_supported")

        if user_input is not None:
            otp = (user_input.get(CONF_OTP) or "").strip()
            errors: dict[str, str] = {}
            if not otp:
                errors["base"] = "missing_otp"
            else:
                import aiohttp
                try:
                    async with aiohttp.ClientSession() as http:
                        client = MadrilenaClient(http)
                        payload = await client.submit_otp(self._login_ctx, otp)
                    session_store = SessionStore(self.hass, self._madrilena_reauth_entry_id)
                    await session_store.async_save_payload(
                        {
                            "cookies": payload.cookies,
                            "csrf_token": payload.csrf_token,
                            "extra": payload.extra,
                        },
                    )
                    entry = self.hass.config_entries.async_get_entry(
                        self._madrilena_reauth_entry_id,
                    )
                    if entry is None:
                        return self.async_abort(reason="reauth_not_supported")
                    return self.async_update_reload_and_abort(
                        entry, reason="reauth_successful",
                    )
                except InvalidOtp:
                    errors["base"] = "invalid_otp"
                except MadrilenaClientNotImplemented:
                    errors["base"] = "client_not_implemented"
                except MadrilenaClientError:
                    _LOGGER.exception("Reauth submit_otp failed")
                    errors["base"] = "login_failed"

            return self.async_show_form(
                step_id="otp",
                data_schema=vol.Schema({vol.Required(CONF_OTP): str}),
                errors=errors,
            )

        return self.async_show_form(
            step_id="otp",
            data_schema=vol.Schema({vol.Required(CONF_OTP): str}),
        )

    @staticmethod
    def _reauth_confirm_schema(default_dni: str = "") -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_DNI, default=default_dni): str,
                vol.Required(CONF_PASSWORD): str,
            },
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
        self._toggle_on_autopilot: bool = False
        self._toggle_off_autopilot: bool = False

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        merged = {**self._entry.data, **self._entry.options}
        previous_autopilot = bool(merged.get(CONF_AUTOPILOT_ENABLED, False))

        if user_input is not None:
            new_autopilot = bool(user_input.get(CONF_AUTOPILOT_ENABLED, False))
            self._pending = {
                CONF_PEOPLE: int(user_input.get(CONF_PEOPLE, 1) or 0),
                CONF_CLIMATE_ENTITIES: list(user_input.get(CONF_CLIMATE_ENTITIES) or []),
                CONF_OUTDOOR_TEMP_ENTITY: (user_input.get(CONF_OUTDOOR_TEMP_ENTITY) or "").strip(),
                CONF_HDD_BASE_C: float(user_input.get(CONF_HDD_BASE_C, DEFAULT_HDD_BASE_C)),
                CONF_ENABLE_COST: bool(user_input.get(CONF_ENABLE_COST, False)),
                CONF_AUTOPILOT_ENABLED: new_autopilot,
            }
            acs_val = user_input.get(CONF_ACS_M3_PER_PERSON_DAY)
            if acs_val and float(acs_val) > 0:
                self._pending[CONF_ACS_M3_PER_PERSON_DAY] = float(acs_val)
            if self._pending[CONF_ENABLE_COST]:
                self._pending[CONF_KWH_PER_M3] = float(user_input.get(CONF_KWH_PER_M3, DEFAULT_KWH_PER_M3))
                self._pending[CONF_PRICE_EUR_KWH] = float(user_input.get(CONF_PRICE_EUR_KWH, 0.07))
            self._toggle_on_autopilot = new_autopilot and not previous_autopilot
            self._toggle_off_autopilot = previous_autopilot and not new_autopilot
            if self._pending[CONF_CLIMATE_ENTITIES]:
                return await self.async_step_areas()
            # No climates selected → drop any stale per-zone areas.
            self._pending[CONF_CLIMATE_AREAS_M2] = {}
            return await self._maybe_step_autopilot_then_finish()

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
                ),
                vol.Required(
                    CONF_AUTOPILOT_ENABLED,
                    default=previous_autopilot,
                ): bool,
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
            return await self._maybe_step_autopilot_then_finish()

        return self.async_show_form(
            step_id="areas",
            data_schema=_areas_schema(entities, current_areas),
            description_placeholders={"count": str(len(entities))},
        )

    async def async_step_autopilot(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Capture portal credentials when autopilot is being turned on.

        We DO NOT trigger ``begin_login`` here — the OTP would expire
        while the user navigates back to options. Instead, we save the
        credentials and let ``async_setup_entry`` (post-reload) start
        the reauth flow if no session is persisted yet.
        """
        from .secrets_store import CredentialsStore

        creds = CredentialsStore(self.hass, self._entry.entry_id)
        await creds.async_load()

        if user_input is not None:
            dni = (user_input.get(CONF_DNI) or "").strip()
            password = (user_input.get(CONF_PASSWORD) or "").strip()
            if not dni or not password:
                return self.async_show_form(
                    step_id="autopilot",
                    data_schema=self._autopilot_schema(creds.dni),
                    errors={"base": "missing_credentials"},
                )
            await creds.async_save(dni=dni, password=password)
            return self.async_create_entry(title="", data=self._pending)

        return self.async_show_form(
            step_id="autopilot",
            data_schema=self._autopilot_schema(creds.dni),
        )

    async def _maybe_step_autopilot_then_finish(self) -> FlowResult:
        """Route to autopilot step if we just toggled it on; else finish."""
        from .secrets_store import CredentialsStore, SessionStore

        if self._toggle_off_autopilot:
            # Autopilot was just disabled — wipe the stored creds and
            # session so nothing lingers on disk.
            try:
                await CredentialsStore(self.hass, self._entry.entry_id).async_clear()
                await SessionStore(self.hass, self._entry.entry_id).async_clear()
            except Exception:
                _LOGGER.exception("Failed to clear autopilot stores on disable")
            return self.async_create_entry(title="", data=self._pending)

        if self._toggle_on_autopilot:
            return await self.async_step_autopilot()

        return self.async_create_entry(title="", data=self._pending)

    @staticmethod
    def _autopilot_schema(default_dni: str = "") -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_DNI, default=default_dni): str,
                vol.Required(CONF_PASSWORD): str,
            },
        )
