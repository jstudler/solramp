# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for sun-altitude based day/night phase detection."""

from __future__ import annotations

from datetime import datetime

import pytest

from solramp.astral_phase import (
    LocationConfig,
    Phase,
    describe_day,
    phase_at,
    sun_altitude,
)

# A clear summer day at the default location gives unambiguous sun motion.
LOC = LocationConfig()
SUMMER = datetime(2024, 6, 21)


def _at(hour: int, minute: int = 0) -> datetime:
    return SUMMER.replace(hour=hour, minute=minute)


def test_morning_is_brightening() -> None:
    assert phase_at(LOC, _at(8)) is Phase.BRIGHTENING


def test_afternoon_is_darkening() -> None:
    assert phase_at(LOC, _at(18)) is Phase.DARKENING


def test_after_solar_midnight_is_brightening() -> None:
    # ~02:00 local: past solar midnight, sun climbing back up.
    assert phase_at(LOC, _at(2)) is Phase.BRIGHTENING


def test_before_solar_midnight_is_darkening() -> None:
    assert phase_at(LOC, _at(23)) is Phase.DARKENING


def test_sun_above_horizon_at_noon_below_at_midnight() -> None:
    assert sun_altitude(LOC, _at(13)) > 0.0
    assert sun_altitude(LOC, _at(1)) < 0.0


def test_naive_datetime_is_localised() -> None:
    # A naive datetime must be treated as local time, not UTC.
    naive = _at(12)
    aware = naive.replace(tzinfo=LOC.tzinfo)
    assert sun_altitude(LOC, naive) == pytest.approx(sun_altitude(LOC, aware))


def test_describe_day_orders_sunrise_before_sunset() -> None:
    info = describe_day(LOC, _at(12))
    assert "sunrise" in info and "sunset" in info
    assert info["sunrise"] < info["sunset"]
    assert "sun_altitude_deg" in info


def test_phase_flips_once_around_solar_noon() -> None:
    # Just the two daytime samples either side of noon must differ.
    assert phase_at(LOC, _at(10)) is Phase.BRIGHTENING
    assert phase_at(LOC, _at(16)) is Phase.DARKENING
