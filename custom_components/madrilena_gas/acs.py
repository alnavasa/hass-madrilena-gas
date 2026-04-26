"""Derive the ACS (Agua Caliente Sanitaria) baseline from history.

Gas in a typical Spanish home goes to two things:

* **ACS** — domestic hot water. Roughly stable across the year; depends
  on number of people, shower habits, kitchen use. Modelled as a
  per-person, per-day m³ figure.

* **Heating** — strongly seasonal. Zero in summer (Jun-Sep) when no one
  turns on the boiler, peaks in Dec-Feb. Distributed across days by
  HDD / climate-on hours.

Strategy: pick the bimestral periods whose dates are fully inside the
summer window (June 1 → Sept 30 by default). In those, gas usage ≈ pure
ACS. Divide by ``people * days`` and take the median across multiple
summer periods to absorb vacation outliers.

If the user has fewer than ``ACS_MIN_SUMMER_PERIODS`` summer periods in
history, fall back to the configured manual override or a sensible
default (≈0.05 m³/person/day for a 4-person household using a combi
boiler).
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from .const import ACS_MIN_SUMMER_PERIODS, ACS_SUMMER_MONTH_END, ACS_SUMMER_MONTH_START
from .models import Period

_LOGGER = logging.getLogger(__name__)

#: Fallback m³/person/day when the user has no summer periods yet.
#: Calibrated from the snapshot we have: 2024 + 2025 summer periods give
#: ~0.24 m³/day for a 4-person household → 0.06 m³/person/day. Bump
#: slightly to 0.07 to err on the safe side (over-attributing to ACS
#: under-attributes to heating, which is the more visible figure).
DEFAULT_ACS_M3_PER_PERSON_DAY = 0.07


@dataclass(frozen=True, slots=True)
class AcsBaseline:
    """Per-person, per-day ACS m³ figure plus provenance for the UI."""

    m3_per_person_day: float
    source: str  # "manual" | "history" | "default"
    summer_periods_used: int = 0


def derive_acs_baseline(
    periods: list[Period],
    people: int,
    *,
    manual_override: float | None = None,
    summer_month_start: int = ACS_SUMMER_MONTH_START,
    summer_month_end: int = ACS_SUMMER_MONTH_END,
) -> AcsBaseline:
    """Pick the best ACS m³/person/day estimate available.

    Priority:
      1. ``manual_override`` if the user pinned a value in OptionsFlow.
      2. Median across summer-only periods divided by people * days.
      3. ``DEFAULT_ACS_M3_PER_PERSON_DAY`` if neither is available.

    The median (rather than mean) defends against the August-vacation
    artefact: a household away for half of August will record very
    little gas in that period and skew a mean down. Median picks the
    typical period instead.
    """
    if people <= 0:
        # Can't divide by zero people — return a safe default flagged
        # as such so the UI can warn.
        _LOGGER.warning("ACS derivation called with people=%s; using default", people)
        return AcsBaseline(DEFAULT_ACS_M3_PER_PERSON_DAY, source="default")

    if manual_override is not None and manual_override > 0:
        return AcsBaseline(float(manual_override), source="manual")

    summer = [p for p in periods if p.is_summer(summer_month_start, summer_month_end) and p.days > 0]
    if len(summer) < ACS_MIN_SUMMER_PERIODS:
        _LOGGER.info(
            "Not enough summer periods (%d < %d) to derive ACS baseline; using default",
            len(summer), ACS_MIN_SUMMER_PERIODS,
        )
        return AcsBaseline(DEFAULT_ACS_M3_PER_PERSON_DAY, source="default")

    per_person_per_day = [p.consumption_m3 / (people * p.days) for p in summer]
    median = statistics.median(per_person_per_day)
    return AcsBaseline(float(median), source="history", summer_periods_used=len(summer))


def acs_m3_for_period(baseline: AcsBaseline, period: Period, people: int) -> float:
    """Total ACS m³ assigned to a period: people × baseline × days.

    Capped at the period's actual consumption — a winter vacation with
    almost no gas use shouldn't end up with negative heating.
    """
    raw = baseline.m3_per_person_day * people * period.days
    return min(raw, period.consumption_m3)
