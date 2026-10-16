# SPDX-License-Identifier: AGPL-3.0-or-later
"""Day/night phase detection from the sun's altitude.

The ramping rule is "only ever darken the image in the afternoon/evening,
only ever brighten it in the morning, and reverse direction at most once per
day".  We get exactly that by keying the *allowed* exposure-change direction to
whether the sun is currently rising or setting:

* sun descending (after solar noon, before solar midnight) -> :attr:`Phase.DARKENING`
* sun ascending  (after solar midnight, before solar noon)  -> :attr:`Phase.BRIGHTENING`

That boundary flips only twice in 24h (at solar noon and solar midnight), which
is exactly the "one reversal per day" budget the spec asks for.

Direction is detected numerically (compare the sun altitude now with a few
minutes later) rather than by comparing against sunrise/sunset clock times, so
it is robust at high latitudes and around the solstices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import elevation, sun


class Phase(Enum):
    """Which way the scene brightness is currently trending."""

    DARKENING = "darkening"  # sun setting: afternoon -> night
    BRIGHTENING = "brightening"  # sun rising: night -> morning


@dataclass(frozen=True)
class LocationConfig:
    """Geographic location used for sun calculations (default: Bern)."""

    # Fundamental point of the Swiss national coordinates (old observatory, Bern).
    latitude: float = 46.951081
    longitude: float = 7.438637
    timezone: str = "Europe/Zurich"
    elevation: float = 542.0  # metres above sea level
    name: str = "Bern"

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def observer(self) -> Observer:
        return Observer(
            latitude=self.latitude,
            longitude=self.longitude,
            elevation=self.elevation,
        )


def _ensure_aware(loc: LocationConfig, when: datetime) -> datetime:
    """Attach the location timezone to a naive datetime."""
    if when.tzinfo is None:
        return when.replace(tzinfo=loc.tzinfo)
    return when


def sun_altitude(loc: LocationConfig, when: datetime) -> float:
    """Sun elevation angle in degrees (negative = below the horizon)."""
    return elevation(loc.observer(), _ensure_aware(loc, when))


def phase_at(
    loc: LocationConfig, when: datetime, *, probe_minutes: float = 10.0
) -> Phase:
    """Return whether the scene is darkening or brightening at ``when``."""
    when = _ensure_aware(loc, when)
    altitude_now = sun_altitude(loc, when)
    altitude_later = sun_altitude(loc, when + timedelta(minutes=probe_minutes))
    # Strictly falling altitude -> darkening; otherwise treat as brightening.
    return Phase.DARKENING if altitude_later < altitude_now else Phase.BRIGHTENING


def describe_day(loc: LocationConfig, when: datetime) -> dict[str, datetime | float]:
    """Best-effort sunrise/sunset/dawn/dusk for logging at start-up.

    Returns whatever astral can compute for the local date of ``when``; on polar
    days where an event does not occur the corresponding key is omitted.
    """
    when = _ensure_aware(loc, when)
    observer = loc.observer()
    local_date = when.astimezone(loc.tzinfo).date()
    result: dict[str, datetime | float] = {
        "sun_altitude_deg": round(sun_altitude(loc, when), 2),
    }
    try:
        events = sun(observer, date=local_date, tzinfo=loc.tzinfo)
        for key in ("dawn", "sunrise", "noon", "sunset", "dusk"):
            if key in events:
                result[key] = events[key]
    except (ValueError, KeyError):
        # Sun never rises / never sets on this date; phase logic still works.
        pass
    return result
