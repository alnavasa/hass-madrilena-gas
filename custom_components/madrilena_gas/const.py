"""Constants for the Madrileña Red de Gas integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "madrilena_gas"

# ---------------------------------------------------------------------
# Config-entry keys
# ---------------------------------------------------------------------

#: Free-text label the user picks in the config flow ("Casa principal").
#: Drives the device name and entity prefix; the unique_id stays tied to
#: the meter number so renaming never breaks history.
CONF_NAME = "name"

#: 48-char hex token (192 bits) generated at config-flow time. Bookmarklet
#: bakes it into the ``Authorization: Bearer …`` header on every POST to
#: the ingest endpoint.
CONF_TOKEN = "token"  # noqa: S105

#: Meter number (numero de contador) bound to this entry. Empty until the
#: first successful POST through the bookmarklet, then auto-set from the
#: scraped page header. Subsequent POSTs whose meter id doesn't match are
#: rejected — same contract-mixing safeguard as Canal.
CONF_METER_ID = "meter_id"

#: Optional override for the HA external URL the bookmarklet POSTs to.
#: Defaults to ``hass.config.external_url``.
CONF_HA_URL = "ha_url"

# ---------------------------------------------------------------------
# Distribution model — how to spread bimonthly consumption across days
# ---------------------------------------------------------------------

#: Number of people living in the household. Drives the ACS (domestic hot
#: water) baseline: a fairly stable per-person, per-day m³ figure that
#: gets subtracted from each bimonthly delta before the rest is treated
#: as space heating and distributed by HDD / climate-on hours.
CONF_PEOPLE = "people"

#: Entity ids of the climate.* / binary_sensor.* entities that drive the
#: heating. The user picks 1..N from their HA. Each entity contributes
#: `area_m² × heating_fraction` per hour to the distribution weight, so
#: hours with many or larger zones active receive more m³ from the
#: bimonthly delta. Empty list = fall back to pure HDD only.
CONF_CLIMATE_ENTITIES = "climate_entities"

#: Per-entity floor area in m². Dict mapping each entity_id picked in
#: ``CONF_CLIMATE_ENTITIES`` to its weight. Default 1.0 per entity =
#: pure zone-count weighting (a 30 m² living room counts the same as
#: a 5 m² bathroom). Setting realistic m² gives the gas-burning weight
#: per zone and produces a noticeably better daily distribution in
#: multi-zone houses (Airzone, etc.).
CONF_CLIMATE_AREAS_M2 = "climate_areas_m2"

#: Default weight when the user leaves a zone area blank — 1.0 means
#: "count this zone as one unit" which mirrors the v0.1.1 zone-count
#: behavior (no measurement needed).
DEFAULT_AREA_M2 = 1.0

#: Entity id of the outdoor temperature sensor. Auto-detect from
#: weather.* entities (Met.no by default exists in nearly all HA installs)
#: but the user can override with sensor.* if they have a dedicated probe.
CONF_OUTDOOR_TEMP_ENTITY = "outdoor_temp_entity"

#: Heating Degree Days base temperature in °C. Standard for Spanish gas
#: heating modelling is 18 °C (REE / IDAE). Hours where outdoor temp ≥
#: this value contribute zero weight to the heating distribution.
CONF_HDD_BASE_C = "hdd_base_c"
DEFAULT_HDD_BASE_C = 18.0

#: Manual override for ACS (domestic hot water) m³/person/day. When unset,
#: the integration auto-derives it from summer periods (Jun-Aug, no
#: heating) in the user's history. Exposed in the OptionsFlow so a user
#: with weird habits (gym showers, vacation absences) can pin it.
CONF_ACS_M3_PER_PERSON_DAY = "acs_m3_per_person_day"

# ---------------------------------------------------------------------
# Cost feature (opt-in, future iteration)
# ---------------------------------------------------------------------

#: Whether to compute cost-derived entities. Off by default. Madrileña is
#: a distributor — the user's commercializadora (typically Endesa,
#: Naturgy, etc.) sets the actual €/kWh, so cost is a manual config.
CONF_ENABLE_COST = "enable_cost"

#: Conversion factor m³ → kWh for natural gas. Madrileña uses the same
#: standard PCS as the rest of Spain; varies slightly per redistricting
#: but ~11.7 kWh/m³ is the long-term average. User can override.
CONF_KWH_PER_M3 = "kwh_per_m3"
DEFAULT_KWH_PER_M3 = 11.70

#: User-configured marginal price in €/kWh (TUR or commercializadora
#: rate). Interpretation depends on ``CONF_COST_MODE``:
#:
#: * ``"simple"`` — treat as the all-in €/kWh (IVA included). The
#:   integration multiplies it by ``kwh_per_m3 × m³`` and stops there.
#:   No fixed term, no rental, no IEH, no IVA arithmetic. Use this
#:   when the user just wants a rough cost trend.
#: * ``"advanced"`` — treat as the variable €/kWh **without IVA**, as
#:   it appears on the invoice (Endesa "Término Energía Gas"). The
#:   integration adds the fixed term, meter rental, IEH and IVA on
#:   top to reproduce the bill total.
CONF_PRICE_EUR_KWH = "price_eur_kwh"

#: ``"simple"`` (default) keeps v0.2.4 behaviour: ``price_eur_kwh × m³``.
#: ``"advanced"`` enables the full Spanish gas-bill formula with fixed
#: term, meter rental, IEH and IVA — the only mode that actually matches
#: the invoice total.
CONF_COST_MODE = "cost_mode"
COST_MODE_SIMPLE = "simple"
COST_MODE_ADVANCED = "advanced"

#: Término fijo gas (Eur/día). On the Endesa "Tarifa One Gas" RL.2 peaje
#: it lands around 0,46 €/día (2026). Pre-IVA, pre-discount.
CONF_TERM_FIJO_EUR_DIA = "term_fijo_eur_dia"

#: Alquiler de equipos / contador (Eur/mes). Madrileña Red de Gas charges
#: a residential meter rental at roughly 0,58 €/mes; the integration
#: divides by 30 to get a per-day contribution. Pre-IVA.
CONF_ALQUILER_EUR_MES = "alquiler_eur_mes"

#: Impuesto Especial sobre Hidrocarburos (Eur/kWh). Fixed nationally for
#: gas natural — 0,001080 €/kWh as of 2026. Pre-IVA.
CONF_IEH_EUR_KWH = "ieh_eur_kwh"
DEFAULT_IEH_EUR_KWH = 0.00108

#: IVA aplicable. Spanish natural gas uses the *reduced* 10 % rate (not
#: the standard 21 %). Default reflects that; user can override if rules
#: change.
CONF_IVA_PCT = "iva_pct"
DEFAULT_IVA_PCT = 10.0

#: Promotional discount (%) applied to the variable term only. Endesa's
#: "Tarifa One" comes with a stack of -10 % discounts; sum them and put
#: the total here (e.g. 20 for two stacked -10 % lines).
CONF_DESCUENTO_PCT = "descuento_pct"
DEFAULT_DESCUENTO_PCT = 0.0

# ---------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------

DEFAULT_NAME = "Madrileña Red de Gas"

#: Coordinator tick interval. The ingest endpoint pokes the coordinator
#: on every POST so live data arrives instantly; this slow tick exists
#: only to refresh derived attributes (current period day index,
#: data_age_minutes, etc.).
UPDATE_INTERVAL = timedelta(hours=1)

#: Source identifier for long-term external statistics
#: (``madrilena_gas:consumption_<meter_id>``).
STATISTICS_SOURCE = DOMAIN

# ---------------------------------------------------------------------
# Ingest endpoint
# ---------------------------------------------------------------------

INGEST_URL_PREFIX = "/api/madrilena_gas/ingest"
BOOKMARKLET_PAGE_URL_PREFIX = "/api/madrilena_gas/bookmarklet"

#: Max POST body size. The /consumos page is ~48 KB per page, two pages
#: give ~100 KB. 4 MB ceiling for the malformed-bookmarklet case
#: (e.g. someone POSTs the whole portal).
MAX_INGEST_BYTES = 4 * 1024 * 1024

# ---------------------------------------------------------------------
# Autopilot mode (v0.2.0 — scaffold, NOT functional yet)
# ---------------------------------------------------------------------

#: Opt-in toggle in OptionsFlow. When True, the auto-fetch coordinator
#: starts on entry setup, polls /consumos every ``AUTOPILOT_POLL_INTERVAL``,
#: and the bookmarklet flow becomes redundant (still works in parallel).
#: When False (default) the integration behaves exactly as v0.1.x.
CONF_AUTOPILOT_ENABLED = "autopilot_enabled"

#: User's portal credentials. NEVER stored in ``entry.data`` — they live
#: in a separate per-entry HA Store (see ``secrets_store.py``) so they
#: don't surface in diagnostics dumps or config-entry exports.
CONF_DNI = "dni"
CONF_PASSWORD = "password"  # noqa: S105
CONF_OTP = "otp"

#: Re-fetch cadence. Empirically the Madrileña Laravel session has a
#: sliding TTL ≈ 80–160 min idle (probe 2026-04-26 survived 7.34h
#: with 80-min polls, died at first 160-min poll). 40 min sits well
#: inside the safe band so the cookie never lapses while HA is online.
AUTOPILOT_POLL_INTERVAL = timedelta(minutes=40)

#: After a transient HTTP error, back off this long before retrying.
#: Avoids hammering the portal during outages.
AUTOPILOT_BACKOFF_INTERVAL = timedelta(minutes=10)

#: Public base URL of the Oficina Virtual. Used by the autopilot client
#: only — the bookmarklet flow doesn't need it (the user's browser
#: already sits on the right origin).
MADRILENA_PORTAL_BASE = "https://ov.madrilena.es"

# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------

STORAGE_KEY_PREFIX = DOMAIN
STORAGE_VERSION = 1

#: Per-entry on-disk store for the user's portal credentials. Separate
#: file from the readings so a diagnostics dump of the readings store
#: never leaks the DNI/password.
STORAGE_SECRETS_KEY_PREFIX = f"{DOMAIN}.secrets"

#: Per-entry on-disk store for the Laravel session cookies + CSRF token
#: used by the autopilot client. Cleared on re-auth and when the user
#: disables autopilot.
STORAGE_SESSION_KEY_PREFIX = f"{DOMAIN}.session"

#: Hard cap on stored readings per entry. Bimonthly readings = ~6/year,
#: so 1000 covers 160+ years. Set generous; readings are tiny.
MAX_READINGS_PER_ENTRY = 1000

# ---------------------------------------------------------------------
# Heuristics for ACS auto-derivation
# ---------------------------------------------------------------------

#: A bimestral period is "summer" if it's fully contained between these
#: months (inclusive). Used to derive the per-person ACS baseline from
#: history: in those periods, gas consumption ≈ pure ACS (no heating).
ACS_SUMMER_MONTH_START = 6   # June
ACS_SUMMER_MONTH_END = 9     # September (period must end by Sep 30)

#: Minimum number of summer periods required to trust the auto-derived
#: ACS baseline. Below this, the integration warns and asks the user to
#: set the value manually in the OptionsFlow.
ACS_MIN_SUMMER_PERIODS = 1

# ---------------------------------------------------------------------
# Open-Meteo Archive API (for backfill of pre-install temperatures)
# ---------------------------------------------------------------------

#: Free, no-key historical reanalysis (ERA5). Gives daily mean
#: temperature per lat/lon. Used once at install time to back-fill HDD
#: for the periods covered by the snapshot but not yet recorded by HA.
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# ---------------------------------------------------------------------
# Dispatcher signals
# ---------------------------------------------------------------------

#: Fired when the user changes meter (e.g. distributor swap). Format
#: with ``entry_id`` and ``meter_id``. Sensors clear their monotonic
#: guards so the new (lower) reading isn't suppressed.
SIGNAL_METER_RESET = "madrilena_gas_meter_reset_{entry_id}_{meter_id}"
