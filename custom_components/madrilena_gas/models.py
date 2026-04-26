"""Pure data models for Madrileña Gas. No HA imports.

Kept dependency-free so the parser, distribution and ACS modules can be
unit-tested without spinning up Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Self


class ReadingType(StrEnum):
    """Reading type as reported by the Madrileña portal.

    The portal uses these labels in the "Tipo" column. ``REAL`` and
    ``REVISADA`` are trustworthy; ``ESTIMADA`` is the distributor's
    guess when no physical lecture happened that bimester. ``FACILITADA``
    is the customer's self-submitted reading via web/WhatsApp.
    """

    REAL = "Real"
    ESTIMADA = "Estimada"
    REVISADA = "Revisada"
    FACILITADA = "Facilitada"
    UNKNOWN = "Unknown"

    @classmethod
    def from_label(cls, label: str) -> Self:
        normalized = label.strip().capitalize()
        for member in cls:
            if member.value == normalized:
                return member  # type: ignore[return-value]
        return cls.UNKNOWN  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class Reading:
    """A single meter reading on a given date.

    The portal reports cumulative meter values (the same number etched
    on the dial, in m³). Consumption is always derived as
    ``Reading[i].lectura_m3 - Reading[i-1].lectura_m3``.
    """

    fecha: date
    lectura_m3: float
    tipo: ReadingType

    def to_dict(self) -> dict:
        return {
            "fecha": self.fecha.isoformat(),
            "lectura_m3": self.lectura_m3,
            "tipo": self.tipo.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(
            fecha=date.fromisoformat(data["fecha"]),
            lectura_m3=float(data["lectura_m3"]),
            tipo=ReadingType.from_label(data.get("tipo", "Unknown")),
        )


@dataclass(frozen=True, slots=True)
class Period:
    """A bimestral consumption period bounded by two consecutive readings.

    ``start`` is the date of the *previous* reading (the floor of this
    period). ``end`` is the date of the *current* reading. Days inside
    the period: ``end - start`` (the start day belongs to the previous
    period; the end day belongs to this one).
    """

    start: date
    end: date
    start_m3: float
    end_m3: float
    end_tipo: ReadingType

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def consumption_m3(self) -> float:
        return self.end_m3 - self.start_m3

    @property
    def avg_m3_per_day(self) -> float:
        d = self.days
        return self.consumption_m3 / d if d > 0 else 0.0

    def is_summer(self, month_start: int, month_end: int) -> bool:
        """True if both start and end fall within the summer window.

        Used to mark periods where consumption can be assumed pure ACS
        (no space heating). The window is inclusive on both ends; e.g.
        with ``(6, 9)`` a period running 18 Jun → 19 Aug counts.
        """
        return (
            month_start <= self.start.month <= month_end
            and month_start <= self.end.month <= month_end
        )


@dataclass(frozen=True, slots=True)
class DailyShare:
    """Result of distributing a Period: one entry per calendar day.

    ``acs_m3`` is the constant per-person * people * 1 day component.
    ``heating_m3`` is the residual after ACS, weighted across days by
    HDD * climate-on multiplier. They sum to the day's total m³ in
    that period.
    """

    day: date
    acs_m3: float
    heating_m3: float
    weight: float = 0.0  # raw heating weight before normalisation, kept for debug

    @property
    def total_m3(self) -> float:
        return self.acs_m3 + self.heating_m3


@dataclass(slots=True)
class DistributionResult:
    """Aggregate output of distributing one Period across its days."""

    period: Period
    daily: list[DailyShare] = field(default_factory=list)
    acs_total_m3: float = 0.0
    heating_total_m3: float = 0.0
    fallback_uniform: bool = False  # True when no HDD/climate data → split linearly

    def by_day(self) -> dict[date, DailyShare]:
        return {d.day: d for d in self.daily}


@dataclass(frozen=True, slots=True)
class ClimateActivityHour:
    """One hour of climate activity for the heating distribution.

    ``heating_fraction`` ∈ [0, 1]: how much of that hour at least one
    of the configured climate.* entities was actively calling for heat.
    A simple model: 0 if all climates were off / cooling; 1 if any was
    in ``heat`` or ``auto`` with current_temp < target_temp; partial if
    the state changed mid-hour.
    """

    hour_start: datetime
    heating_fraction: float


@dataclass(frozen=True, slots=True)
class DailyWeather:
    """One day of outdoor weather, used for HDD computation."""

    day: date
    mean_temp_c: float

    def hdd(self, base_c: float) -> float:
        return max(0.0, base_c - self.mean_temp_c)
