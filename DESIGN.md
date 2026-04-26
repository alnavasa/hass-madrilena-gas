# Madrileña Red de Gas — Architecture

Home Assistant custom integration for Madrid's gas distributor (Madrileña Red de Gas). Pulls bimestral consumption from the customer portal `ov.madrilena.es/consumos` and feeds the Energy panel with a daily breakdown of total gas, ACS (domestic hot water) and heating.

## §0 Why this design

The portal has three properties that ruled out every "scrape from HA" approach:

1. **2FA email loop on every fresh login.** The portal sends a 6-digit OTP and there is no "remember this device" / refresh-token flow. A long-running scraper would page the user every session expiry.
2. **Laravel session cookie with a short, non-rolling TTL.** Even if a cookie is captured, it expires within minutes-to-hours and cannot be refreshed without going through the OTP again.
3. **Bimestral cadence.** The portal only updates ~6 times a year. Polling makes no sense — this is push-shaped data.

The integration adopts the **bookmarklet pattern**: the user pastes a one-line `javascript:…` favorite into their browser. While they are already logged into the portal, one click reads the rendered HTML of `/consumos` and `POST`s it to a Bearer-token-protected HA endpoint. HA never owns a session cookie, never sees the OTP, never scrapes anything. The pattern is ported from the sister integration `alnavasa/hass-canal-isabel-ii` (water).

## §1 Module map

```
custom_components/madrilena_gas/
├── __init__.py              setup_entry / unload_entry / services
├── manifest.json            HACS manifest
├── const.py                 CONF_ keys, defaults, URLs
├── models.py                Reading, Period, DailyShare, DailyWeather, ClimateActivityHour
├── store.py                 ReadingStore (per-entry persisted readings)
├── parser.py                HTML → Reading (+meter_id)
├── ingest.py                HTTP POST endpoint (bookmarklet target)
├── bookmarklet.py           JS template + page renderer
├── bookmarklet_view.py      HTTP GET install page (?t=<token>-protected)
├── coordinator.py           DataUpdateCoordinator + CoordinatorData snapshot
├── recorder_helpers.py      HA recorder reads (outdoor temp + climate hours)
├── weather_history.py       Open-Meteo Archive fallback (no API key)
├── distribution.py          Bimestral total → per-day shares (HDD × climate)
├── acs.py                   ACS baseline derivation from summer periods
├── statistics_helpers.py    daily shares → cumulative streams
├── statistics_push.py       async_add_external_statistics wrapper
├── sensor.py                9 sensor classes (state + diagnostic)
├── config_flow.py           Wizard + OptionsFlow
├── strings.json             Source (Spanish) translations
└── translations/
    ├── es.json
    └── en.json
```

## §2 Data flow

```
       ┌─────────────────┐  user click in browser
       │ ov.madrilena.es │  (already logged in, has session cookie)
       └────────┬────────┘
                │  javascript:fetch('/consumos').then(POST)
                ▼
   ┌───────────────────────────┐
   │ MadrilenaGasIngestView    │  /api/madrilena_gas/ingest/<entry_id>
   │  - Bearer token check     │  Authorization: Bearer <48-hex token>
   │  - 4 MB body cap          │
   │  - per-entry asyncio.Lock │
   └────────────┬──────────────┘
                │ parse_pages()  → list[Reading], meter_id
                ▼
   ┌───────────────────────────┐
   │ ReadingStore (per entry)  │  storage_v1, JSON file in HA storage
   │  - dedup by (date, m³)    │
   │  - sort newest-first      │
   │  - atomic save            │
   └────────────┬──────────────┘
                │ async_request_refresh()  (or async_reload on first ingest)
                ▼
   ┌───────────────────────────┐         ┌──────────────────────┐
   │ MadrilenaGasCoordinator   │←────────│ HA recorder          │
   │  - build_periods()        │         │ outdoor temp + climates
   │  - derive_acs_baseline()  │         └──────────────────────┘
   │  - distribute_period()×N  │←────────┐
   │  - signature cache        │         │
   └────────────┬──────────────┘         │
                │                        │
                │ for missing days       │
                ▼                        │
   ┌───────────────────────────┐         │
   │ Open-Meteo Archive        │─────────┘
   │  ERA5 reanalysis, no key  │
   └───────────────────────────┘
                │
                ▼
   ┌───────────────────────────┐         ┌──────────────────────┐
   │ push_distribution_streams │────────→│ recorder.statistics  │
   │  total / acs / heating    │         │ Energy panel reads   │
   │  external_statistics      │         │  this, NOT sensors   │
   └────────────┬──────────────┘         └──────────────────────┘
                │
                ▼
   ┌───────────────────────────┐
   │ CoordinatorData snapshot  │  →  9 SensorEntity classes
   └───────────────────────────┘
```

## §3 Key design decisions

### 3.1 Bookmarklet auth model

Each entry generates a 192-bit hex token (`secrets.token_hex(24)`) at config-flow time. The bookmarklet bakes it into an `Authorization: Bearer <token>` header on every POST. The ingest view validates with `secrets.compare_digest` (constant-time).

The same token is also accepted as `?t=<token>` on the GET install page (`bookmarklet_view.py`). This is symmetric: the token already lives inside the bookmarklet body, so URL exposure is no worse than copying it. The install page is `requires_auth=False` because the user reaches it from a Markdown link in a persistent notification (a plain browser navigation that can't carry HA's frontend Bearer header).

### 3.2 First-ingest reload, subsequent ingest refresh

The first POST binds `meter_id` to `entry.data` and calls `hass.config_entries.async_reload()`. This is necessary because `sensor.py::async_setup_entry` early-exits when no meter is bound — entities can't be created without a stable device unique_id. The reload re-runs setup with the meter id present and entities materialise.

Subsequent POSTs only call `coordinator.async_request_refresh()`. No reload, no entity churn — just fresh data through the existing coordinator.

The `meter_id` mismatch case (user opens a different contract in the portal and clicks the same bookmarklet) returns HTTP 409 with a persistent notification — same contract-mixing safeguard as Canal.

### 3.3 Coordinator without portal I/O

`MadrilenaGasCoordinator` does **zero** I/O against `ov.madrilena.es`. Its `_async_update_data` reads the in-memory store, computes derived state, hits the HA recorder + Open-Meteo for temperatures, and pushes statistics. The 1 h tick exists only to refresh derived attributes (period day index, `data_age_days`, late recorder data). Real updates arrive via the ingest path.

A signature cache `(num_readings, last_reading, options-relevant fields)` skips the whole recompute when nothing changed since the last tick.

### 3.4 Bimestral → daily distribution

Madrileña reports one reading every ~60 days. The Energy panel wants daily curves. `distribute_period()` spreads a bimestral total over its days using:

```
weight(day) = climate_on_hours(day) × HDD(day, base=18°C)
```

Falling back to plain HDD when no climate entities are configured, and to a uniform split when neither weather nor climates are available. This means a brand-new install with no recorder history still gets *some* curve in the Energy panel (better than zero).

ACS is constant per day (`baseline.m3_per_person_day × people`), subtracted from each period total before distributing the heating residual by weight. Capped at the period total so a winter vacation can't produce negative heating.

### 3.5 ACS baseline auto-derivation

`derive_acs_baseline()` picks bimestral periods fully contained in summer (Jun–Sep). In those months gas usage ≈ pure ACS (the boiler is off for heating). Per-person, per-day = `period_total / (people × period.days)`. The **median** across summer periods absorbs the August-vacation outlier. Falls back to `0.07 m³/person/day` if no summer periods exist yet, or to the user's manual override (OptionsFlow).

### 3.6 Two-tier weather strategy

Per-period temperatures come from:

1. **HA recorder** for the user's `outdoor_temp_entity` (sensor.* or weather.*).
2. **Open-Meteo Archive** (ERA5 reanalysis, free, no API key) for days the recorder doesn't have — typically the months *before* the integration was installed.

The recorder wins on overlapping days. Open-Meteo only fills the gaps. `lat`/`lon` come from `hass.config`.

### 3.7 Climate state aggregation

`fetch_climate_hours_from_recorder()` walks state spans from the recorder, distributes each "heating on" segment across hour buckets, and takes the **max** across multiple climate entities (so two thermostats running simultaneously don't double-count). "Heating on" = `hvac_action == "heating"` if available, else state in `{heat, heat_cool, auto}` as a fallback for entities without `hvac_action`.

### 3.8 Long-term statistics, not native sum

The Energy panel reads three external statistics streams (one cumulative sum each):

* `madrilena_gas:total_<meter>`
* `madrilena_gas:acs_<meter>`
* `madrilena_gas:heating_<meter>`

These are pushed via `recorder.async_add_external_statistics` on every coordinator refresh. The recorder upserts by `(statistic_id, start)`, so re-pushing the full history is idempotent and cheap. Sensors keep their own state for the device card / templates / automations, but **the Energy panel does not read sensors** — it reads these streams directly.

Why external statistics rather than `state_class=TOTAL_INCREASING` on a regular sensor? Because the integration retroactively rewrites past days as new readings arrive (a bimestral period covers ~60 days that get redistributed when the closing reading lands). External statistics let us upsert any past timestamp; native sensor sum only accepts forward-moving values.

### 3.9 Per-entry asyncio.Lock for ingest

Two POSTs hitting the same entry within milliseconds (double-click, browser retry) would otherwise race the entry-data update + store write + reload. The lock is per-entry (not global) so two different installations can ingest in parallel.

## §4 Configuration

### 4.1 Wizard (config flow)

| Field | Default | Notes |
|---|---|---|
| `name` | `"Madrileña Red de Gas"` | Free text, drives device name |
| `ha_url` | `hass.config.external_url` | Where the bookmarklet POSTs |
| `people` | required | Drives ACS baseline |
| `climate_entities` | `[]` | Multi-select climate.* |
| `outdoor_temp_entity` | optional | sensor.* or weather.* |
| `hdd_base_c` | `18.0` | Spanish standard |
| `enable_cost` | `False` | Opt-in second step |

If `enable_cost=True`, a second step asks `kwh_per_m3` (default 11.70) and `price_eur_kwh`.

### 4.2 OptionsFlow (post-install)

All wizard fields plus:

| Field | Notes |
|---|---|
| `acs_m3_per_person_day` | Manual override for summer-derived baseline (0/empty = auto) |

Changes trigger `async_reload` so the coordinator picks up new climate/weather entities cleanly.

## §5 Storage

`ReadingStore` keeps one JSON file per entry in HA's storage:

```json
{
  "version": 1,
  "meter_id": "1234567",
  "readings": [{"fecha": "2025-03-15", "lectura_m3": 8421, "tipo": "Real"}, ...],
  "last_ingest_at": "2026-04-26T09:20:00Z"
}
```

Newest-first, dedup by `(date, m³)`, hard cap at 1000 readings (= ~160 years of bimestral readings — readings are tiny). Atomic save via HA's storage helper.

## §6 Services

| Service | Description |
|---|---|
| `madrilena_gas.refresh` | Re-runs the coordinator on cached data. Does NOT hit the portal. |
| `madrilena_gas.show_bookmarklet` | Re-publishes the install notification. |

Both accept an optional `instance` field (entry name or entry_id) to scope to one installation.

## §7 What's NOT in this integration

* **No portal scraping from HA.** No cookie jar, no headless browser, no `requests.Session`. The portal's 2FA + short cookie TTL make this a non-starter; the bookmarklet sidesteps it entirely.
* **No IMAP/email OTP automation.** Hard-rejected. Reading mail to extract the OTP code is fragile, security-hostile, and against the spirit of the integration.
* **No write back to the portal.** Read-only.
* **No live cost panel.** Cost computation is opt-in and uses fixed `€/kWh` × `kWh/m³` from config — no real-time tariff lookup.

## §8 Test surface

`tests/`:

* `test_parser.py` — HTML → Reading + meter_id, Spanish number parsing, dedup
* `test_distribution.py` — period building, HDD weighting, climate override, real-data reconcile
* `test_acs.py` — manual override, summer derivation, default fallback, period cap

15 tests, all green. The coordinator/sensor wiring is exercised end-to-end via `pytest-homeassistant-custom-component` once the integration goes through HACS install testing.

## §9 Sister project

The sister integration `alnavasa/hass-canal-isabel-ii` (Madrid water) shares the same bookmarklet pattern, ingest view shape, and storage layout. Different portal, different units, different tariff model — but the architecture is intentionally parallel so a fix in one informs the other.
