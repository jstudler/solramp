# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-numpy frame metrics for exposure / clipping analysis.

This module deliberately depends only on numpy so it can be unit-tested and
benchmarked with synthetic arrays.  :mod:`solramp.raw_analysis` decodes a CR2
into a linear-light RGB array and feeds it to :func:`analyze_rgb` here.

All metrics are expressed in a way that is convenient for ETTR-style ramping:

* ``clipping_white_pct`` / ``clipping_black_pct`` - percentage of pixels whose
  brightest / all channels are saturated at the top / crushed at the bottom.
* ``mean_ev`` / ``highlight_ev`` - mean and high-percentile luminance expressed
  as *stops below sensor saturation* (always <= 0; 0 == fully blown out).  These
  are the control signals the ramping engine uses to hit an exposure target.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Rec.709 / sRGB luminance weights, applied to linear-light RGB.
_LUMA_WEIGHTS = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

# Smallest luminance we are willing to take log2() of, to avoid -inf on a
# completely black frame.
_EPS = 1e-6


@dataclass(frozen=True)
class FrameMetrics:
    """Exposure metrics derived from a single frame's linear RGB data."""

    clipping_white_pct: float
    clipping_black_pct: float
    mean_ev: float  # mean luminance, stops below saturation (<= 0)
    highlight_ev: float  # high-percentile luminance, stops below saturation (<= 0)
    mean_saturation: float  # mean per-pixel colour saturation, 0..1

    def headroom_stops(self) -> float:
        """Stops between the bright highlights and clipping (>= 0 is good)."""
        return -self.highlight_ev


def analyze_rgb(
    rgb: np.ndarray,
    *,
    white_level: float = 65535.0,
    white_clip_frac: float = 0.99,
    black_clip_frac: float = 0.01,
    highlight_percentile: float = 99.5,
) -> FrameMetrics:
    """Compute :class:`FrameMetrics` from a linear-light ``H x W x 3`` array.

    Parameters
    ----------
    rgb:
        Linear (gamma 1.0) RGB image, any integer or float dtype.  Values are
        interpreted on a ``0 .. white_level`` scale.
    white_level:
        Value corresponding to a fully saturated sensor pixel (e.g. 65535 for
        16-bit output from ``rawpy.postprocess(output_bps=16)``).
    white_clip_frac:
        A pixel counts as highlight-clipped if its *brightest* channel is at or
        above ``white_clip_frac * white_level``.  Highlight clipping is judged
        per channel because saturated colours (a setting sun) blow one channel
        first.
    black_clip_frac:
        A pixel counts as black-clipped if its *brightest* channel is at or
        below ``black_clip_frac * white_level`` (i.e. every channel is crushed).
    highlight_percentile:
        Percentile of luminance used as the ETTR highlight indicator.
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"expected H x W x 3 array, got shape {rgb.shape!r}")
    if rgb.size == 0:
        raise ValueError("cannot analyse an empty image")

    data = rgb.astype(np.float64, copy=False)
    white_thr = white_clip_frac * white_level
    black_thr = black_clip_frac * white_level

    # Per-pixel brightest channel drives highlight/black clipping decisions.
    channel_max = data.max(axis=2)
    total = channel_max.size
    clipping_white_pct = float(np.count_nonzero(channel_max >= white_thr)) / total * 100.0
    clipping_black_pct = float(np.count_nonzero(channel_max <= black_thr)) / total * 100.0

    # Luminance for the exposure indicators.
    luminance = data.reshape(-1, 3) @ _LUMA_WEIGHTS
    mean_lum = float(luminance.mean())
    highlight_lum = float(np.percentile(luminance, highlight_percentile))

    mean_ev = math.log2(max(mean_lum, _EPS) / white_level)
    highlight_ev = math.log2(max(highlight_lum, _EPS) / white_level)

    # Mean colour saturation (HSV "S"): (max-min)/max per pixel.
    channel_min = data.min(axis=2)
    with np.errstate(divide="ignore", invalid="ignore"):
        sat = np.where(channel_max > 0, (channel_max - channel_min) / channel_max, 0.0)
    mean_saturation = float(sat.mean())

    return FrameMetrics(
        clipping_white_pct=clipping_white_pct,
        clipping_black_pct=clipping_black_pct,
        mean_ev=mean_ev,
        highlight_ev=highlight_ev,
        mean_saturation=mean_saturation,
    )
