# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decode a Canon CR2 raw file into exposure metrics using rawpy.

This is the only module that touches rawpy/libraw.  It is intentionally thin:
it decodes the raw to a *linear-light* RGB array and hands the array to the
pure-numpy :func:`solramp.exposure.analyze_rgb`, which does the real work and
is unit-tested with synthetic arrays.

Postprocessing choices (and why):

* ``no_auto_bright=True`` - **critical**.  Auto-brightening would normalise every
  frame to roughly the same brightness and destroy the very signal we ramp on.
* ``use_camera_wb=True``  - AWB on the camera is fine because we shoot raw; we
  just reuse the camera's white balance so colours are sane.
* ``gamma=(1, 1)``        - linear output so luminance is photometrically
  meaningful for EV math (no sRGB tone curve baked in).
* ``output_color=sRGB``   - the requested sRGB primaries.
* ``half_size=True``      - demosaic at half resolution: ~4x faster and more than
  accurate enough for exposure/clipping metrics.  Configurable.
"""

from __future__ import annotations

from pathlib import Path

import rawpy

from .exposure import FrameMetrics, analyze_rgb

# 16-bit postprocess output saturates at this value.
_WHITE_LEVEL_16BIT = 65535.0


def decode_linear_rgb(path: str | Path, *, half_size: bool = True):
    """Return a linear-light 16-bit ``H x W x 3`` numpy array for a CR2 file."""
    with rawpy.imread(str(path)) as raw:
        return raw.postprocess(
            output_bps=16,
            no_auto_bright=True,
            use_camera_wb=True,
            gamma=(1, 1),
            output_color=rawpy.ColorSpace.sRGB,
            half_size=half_size,
            user_flip=0,
        )


def analyze_cr2(
    path: str | Path,
    *,
    half_size: bool = True,
    white_clip_frac: float = 0.99,
    black_clip_frac: float = 0.01,
    highlight_percentile: float = 99.5,
) -> FrameMetrics:
    """Decode ``path`` and return its :class:`~solramp.exposure.FrameMetrics`."""
    rgb = decode_linear_rgb(path, half_size=half_size)
    return analyze_rgb(
        rgb,
        white_level=_WHITE_LEVEL_16BIT,
        white_clip_frac=white_clip_frac,
        black_clip_frac=black_clip_frac,
        highlight_percentile=highlight_percentile,
    )
