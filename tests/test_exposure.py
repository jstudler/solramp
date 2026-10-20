# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure-numpy exposure metrics, using synthetic arrays."""

from __future__ import annotations

import math

import numpy as np
import pytest

from solramp.exposure import FrameMetrics, analyze_rgb

from .conftest import WHITE_LEVEL, gradient_rgb, solid_rgb


def test_fully_white_frame_clips_white() -> None:
    metrics = analyze_rgb(solid_rgb(WHITE_LEVEL))
    assert metrics.clipping_white_pct == pytest.approx(100.0)
    assert metrics.clipping_black_pct == pytest.approx(0.0)
    # Highlights sit at saturation -> ~0 EV of headroom.
    assert metrics.highlight_ev == pytest.approx(0.0, abs=1e-6)
    assert metrics.headroom_stops() == pytest.approx(0.0, abs=1e-6)


def test_fully_black_frame_clips_black() -> None:
    metrics = analyze_rgb(solid_rgb(0.0))
    assert metrics.clipping_black_pct == pytest.approx(100.0)
    assert metrics.clipping_white_pct == pytest.approx(0.0)
    # log2 is floored via epsilon, so EV is a large negative number, not -inf.
    assert math.isfinite(metrics.mean_ev)
    assert metrics.mean_ev < -20.0


def test_mid_gray_is_one_stop_below_saturation() -> None:
    metrics = analyze_rgb(solid_rgb(WHITE_LEVEL / 2.0))
    # Uniform gray: luminance == channel value, so EV == log2(0.5) == -1.
    assert metrics.mean_ev == pytest.approx(-1.0, abs=1e-3)
    assert metrics.highlight_ev == pytest.approx(-1.0, abs=1e-3)
    assert metrics.clipping_white_pct == pytest.approx(0.0)
    assert metrics.clipping_black_pct == pytest.approx(0.0)


def test_quarter_gray_is_two_stops_below() -> None:
    metrics = analyze_rgb(solid_rgb(WHITE_LEVEL / 4.0))
    assert metrics.mean_ev == pytest.approx(-2.0, abs=1e-3)


def test_white_clip_threshold_is_per_channel() -> None:
    # One channel saturated, others mid: counts as white-clipped.
    rgb = solid_rgb(WHITE_LEVEL / 2.0)
    rgb[..., 0] = WHITE_LEVEL  # blow the red channel only
    metrics = analyze_rgb(rgb)
    assert metrics.clipping_white_pct == pytest.approx(100.0)


def test_partial_clipping_percentage() -> None:
    # Half the pixels fully white, half mid-gray.
    rgb = solid_rgb(WHITE_LEVEL / 2.0, shape=(10, 10))
    rgb[:5, :, :] = WHITE_LEVEL
    metrics = analyze_rgb(rgb)
    assert metrics.clipping_white_pct == pytest.approx(50.0)


def test_saturation_zero_for_gray_one_for_pure_color() -> None:
    gray = analyze_rgb(solid_rgb(WHITE_LEVEL / 2.0))
    assert gray.mean_saturation == pytest.approx(0.0)

    pure = solid_rgb(0.0)
    pure[..., 0] = WHITE_LEVEL  # pure red -> saturation 1
    assert analyze_rgb(pure).mean_saturation == pytest.approx(1.0)


def test_highlight_percentile_tracks_brightest_pixels() -> None:
    rgb = gradient_rgb(0.0, WHITE_LEVEL, shape=(8, 64))
    low = analyze_rgb(rgb, highlight_percentile=50.0).highlight_ev
    high = analyze_rgb(rgb, highlight_percentile=99.5).highlight_ev
    # A higher percentile samples brighter pixels -> closer to 0 EV.
    assert high > low


def test_white_level_scaling() -> None:
    # Same relative brightness on an 8-bit scale yields the same EV.
    metrics = analyze_rgb(solid_rgb(128.0), white_level=255.0)
    assert metrics.mean_ev == pytest.approx(math.log2(128.0 / 255.0), abs=1e-3)


def test_rejects_wrong_shape() -> None:
    with pytest.raises(ValueError):
        analyze_rgb(np.zeros((4, 4)))  # missing channel axis


def test_rejects_empty() -> None:
    with pytest.raises(ValueError):
        analyze_rgb(np.zeros((0, 0, 3)))


def test_returns_frame_metrics_type() -> None:
    assert isinstance(analyze_rgb(solid_rgb(1000.0)), FrameMetrics)
