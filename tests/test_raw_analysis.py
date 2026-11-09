# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the rawpy decoder, using generated raw frames.

The fixtures are built by :mod:`tests.synth_raw` at known pixel levels, so these
assert exact metered values rather than mere inequalities.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from solramp.exposure import FrameMetrics
from solramp.raw_analysis import analyze_cr2, decode_linear_rgb

from .synth_raw import BRIGHT_LEVEL, DARK_LEVEL, expected_mean_ev

# Tolerance for metered EV, in stops.  Demosaic interpolation and the seeded
# shot-noise move the mean by well under this.
EV_TOL = 0.02


def test_decode_returns_linear_rgb(sample_cr2: Path) -> None:
    rgb = decode_linear_rgb(sample_cr2, half_size=True)
    assert rgb.ndim == 3
    assert rgb.shape[2] == 3
    assert rgb.dtype == np.uint16  # 16-bit postprocess output


def test_decode_preserves_written_level(sample_cr2: Path) -> None:
    # Guards the postprocess flags: auto-brightening or a baked-in gamma curve
    # would pull the decoded mean away from the level the fixture was built at.
    rgb = decode_linear_rgb(sample_cr2, half_size=True)
    assert rgb.mean() == pytest.approx(BRIGHT_LEVEL, rel=0.01)


def test_half_size_is_smaller_than_full(sample_cr2: Path) -> None:
    half = decode_linear_rgb(sample_cr2, half_size=True)
    full = decode_linear_rgb(sample_cr2, half_size=False)
    assert half.shape[0] < full.shape[0]
    assert half.shape[1] < full.shape[1]


def test_analyze_cr2_metrics_in_range(sample_cr2: Path) -> None:
    metrics = analyze_cr2(sample_cr2, half_size=True)
    assert isinstance(metrics, FrameMetrics)
    assert 0.0 <= metrics.clipping_white_pct <= 100.0
    assert 0.0 <= metrics.clipping_black_pct <= 100.0
    # Real-world frames are never brighter than saturation.
    assert metrics.highlight_ev <= 0.0
    assert metrics.mean_ev <= metrics.highlight_ev + 1e-6
    assert 0.0 <= metrics.mean_saturation <= 1.0


def test_metered_ev_matches_pixel_level(
    sample_cr2_bright: Path, sample_cr2_dark: Path
) -> None:
    bright = analyze_cr2(sample_cr2_bright, half_size=True)
    dark = analyze_cr2(sample_cr2_dark, half_size=True)
    assert bright.mean_ev == pytest.approx(expected_mean_ev(BRIGHT_LEVEL), abs=EV_TOL)
    assert dark.mean_ev == pytest.approx(expected_mean_ev(DARK_LEVEL), abs=EV_TOL)


def test_bright_frame_is_brighter_than_dark_frame(
    sample_cr2_bright: Path, sample_cr2_dark: Path
) -> None:
    bright = analyze_cr2(sample_cr2_bright, half_size=True)
    dark = analyze_cr2(sample_cr2_dark, half_size=True)
    # The early-evening frame must meter brighter than the late-dusk one, by
    # exactly the stop difference between the two pixel levels.
    assert bright.mean_ev > dark.mean_ev
    expected_gap = expected_mean_ev(BRIGHT_LEVEL) - expected_mean_ev(DARK_LEVEL)
    assert bright.mean_ev - dark.mean_ev == pytest.approx(expected_gap, abs=EV_TOL)


def test_blown_frame_reports_white_clipping(sample_cr2_clipped: Path) -> None:
    metrics = analyze_cr2(sample_cr2_clipped, half_size=True)
    assert metrics.clipping_white_pct == pytest.approx(100.0)
    assert metrics.clipping_black_pct == 0.0
    # Essentially no headroom left before saturation.
    assert metrics.headroom_stops() < 0.1


def test_crushed_frame_reports_black_clipping(sample_cr2_crushed: Path) -> None:
    metrics = analyze_cr2(sample_cr2_crushed, half_size=True)
    assert metrics.clipping_black_pct == pytest.approx(100.0)
    assert metrics.clipping_white_pct == 0.0
    # Many stops of unused headroom: the ramp should be opening up here.
    assert metrics.headroom_stops() > 6.0


def test_full_size_meters_like_half_size(sample_cr2_bright: Path) -> None:
    # Half-size demosaic is the speed/accuracy tradeoff the analyser defaults to;
    # it must not shift the exposure reading it is used to make.
    half = analyze_cr2(sample_cr2_bright, half_size=True)
    full = analyze_cr2(sample_cr2_bright, half_size=False)
    assert half.mean_ev == pytest.approx(full.mean_ev, abs=EV_TOL)

