# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Canon exposure tables and brightness-stop math."""

from __future__ import annotations

import math

import pytest

from solramp.camera_tables import (
    APERTURE_AXIS,
    ISO_AXIS,
    SHUTTER_AXIS,
    ExposureAxis,
    SettingValue,
    fastest_shutter_within,
    shutter_label_to_seconds,
)


@pytest.mark.parametrize(
    "label, seconds",
    [
        ("30", 30.0),
        ("0.6", 0.6),
        ("1", 1.0),
        ("1/4", 0.25),
        ("1/250", 0.004),
        ("1/8000", 1.0 / 8000.0),
    ],
)
def test_shutter_label_to_seconds(label: str, seconds: float) -> None:
    assert shutter_label_to_seconds(label) == pytest.approx(seconds)


def test_axes_ordered_dark_to_bright() -> None:
    # Index 0 is always the darkest value, last index the brightest.
    assert ISO_AXIS.labels[0] == "100"
    assert ISO_AXIS.labels[-1] == "32000"
    # f/22 is the darkest aperture, f/2.8 the brightest.
    assert APERTURE_AXIS.labels[0] == "22"
    assert APERTURE_AXIS.labels[-1] == "2.8"
    # 1/8000 is the darkest shutter, 30s the brightest.
    assert SHUTTER_AXIS.labels[0] == "1/8000"
    assert SHUTTER_AXIS.labels[-1] == "30"


def test_iso_stops_double_per_full_stop() -> None:
    assert ISO_AXIS.stops_of("100") == pytest.approx(0.0)
    assert ISO_AXIS.stops_of("200") == pytest.approx(1.0)
    assert ISO_AXIS.stops_of("400") == pytest.approx(2.0)
    assert ISO_AXIS.stops_between("100", "200") == pytest.approx(1.0)


def test_aperture_stops_sign_and_magnitude() -> None:
    # Wider aperture (smaller f) is brighter == higher stops.
    assert APERTURE_AXIS.stops_of("2.8") > APERTURE_AXIS.stops_of("22")
    # One whole f-stop f/2.8 -> f/4 halves the light (~-1 stop; the "2.8" label
    # is really sqrt(8)=2.828, so the exact value is -1.03).
    assert APERTURE_AXIS.stops_between("2.8", "4") == pytest.approx(-1.0, abs=0.05)


def test_shutter_stops_match_log2_seconds() -> None:
    assert SHUTTER_AXIS.stops_of("1") == pytest.approx(0.0)
    assert SHUTTER_AXIS.stops_of("2") == pytest.approx(1.0)
    assert SHUTTER_AXIS.stops_of("0.5") == pytest.approx(-1.0)


def test_step_clamps_to_limits() -> None:
    # Stepping brighter past the bright limit clamps at the limit label.
    assert ISO_AXIS.step("32000", 5) == "32000"
    # Stepping darker past index 0 clamps at the darkest label.
    assert ISO_AXIS.step("100", -5) == "100"
    # Optional min/max labels constrain the range (in brightness order).
    assert ISO_AXIS.step("100", 10, max_label="400") == "400"
    assert ISO_AXIS.step("800", -10, min_label="400") == "400"


def test_step_moves_expected_distance() -> None:
    one_up = ISO_AXIS.step("100", 1)
    assert ISO_AXIS.index_of(one_up) == ISO_AXIS.index_of("100") + 1


def test_can_step_detects_clamping() -> None:
    assert ISO_AXIS.can_step("100", 1) is True
    assert ISO_AXIS.can_step("32000", 1) is False
    assert ISO_AXIS.can_step("100", -1) is False


def test_invalid_label_raises() -> None:
    with pytest.raises(KeyError):
        ISO_AXIS.index_of("99")


def test_empty_allowed_range_raises() -> None:
    # max brighter-bound darker than min bound -> empty range.
    with pytest.raises(ValueError):
        ISO_AXIS.step("200", 1, min_label="400", max_label="200")


@pytest.mark.parametrize(
    "max_seconds, expected",
    [
        (30.0, "30"),
        (25.0, "25"),
        (15.0, "15"),
        (0.5, "0.5"),
        (0.49, "0.4"),
        (0.00001, "1/8000"),  # below the fastest shutter -> fastest available
    ],
)
def test_fastest_shutter_within(max_seconds: float, expected: str) -> None:
    assert fastest_shutter_within(max_seconds) == expected


def test_duplicate_labels_rejected() -> None:
    with pytest.raises(ValueError):
        ExposureAxis("dup", [SettingValue("a", 0.0), SettingValue("a", 1.0)])


def test_axis_sorts_by_stops_regardless_of_input_order() -> None:
    axis = ExposureAxis(
        "x",
        [SettingValue("bright", 2.0), SettingValue("dark", -2.0), SettingValue("mid", 0.0)],
    )
    assert axis.labels == ["dark", "mid", "bright"]
    assert axis.stops_of("mid") == pytest.approx(0.0)
    assert math.isclose(axis.stops_between("dark", "bright"), 4.0)
