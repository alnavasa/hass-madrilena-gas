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

#: Entity ids of the climate.* entities that drive the heating. The user
#: picks 1..N from the available climates in their HA. When at least one
#: of them is in a heating state (heat/auto with current_temp <
#: target_temp) for an hour, that hour gets weighted in the distribution.
#: Empty list = fall back to pure HDD on outdoor temperature only.
CONF_CLIMATE_ENTITIES = "climate_entities"

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
#: rate). Full bill modelling (term fijo, peaje, IVA) deferred to a
#: future version — keep v0.1 simple.
CONF_PRICE_EUR_KWH = "price_eur_kwh"

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
# Storage
# ---------------------------------------------------------------------

STORAGE_KEY_PREFIX = DOMAIN
STORAGE_VERSION = 1

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
