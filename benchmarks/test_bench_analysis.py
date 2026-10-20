# SPDX-License-Identifier: AGPL-3.0-or-later
"""Performance benchmarks for the local (camera-free) analysis hot paths.

Run with::

    uv run pytest benchmarks/ --benchmark-only

The raw fixture is generated at full sensor resolution by :mod:`tests.synth_raw`
(see that module for why we build raw files instead of committing them), so the
decode benchmarks time a realistic workload.  Nothing here touches the camera.

.. note::

   The generated fixture is *uncompressed*, while a real CR2 is losslessly
   compressed.  Decoding one therefore skips the entropy-decode step: measured
   against a real 5D Mark IV frame, full-size decode is within ~20% but
   half-size is roughly 3x faster.  Treat the absolute decode numbers as a lower
   bound on real-camera timing; they remain valid for spotting regressions.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from solramp.astral_phase import LocationConfig
from solramp.exposure import analyze_rgb
from solramp.ramping import ExposureState, RampParams, RampingEngine
from solramp.raw_analysis import analyze_cr2, decode_linear_rgb

from tests.synth_raw import BRIGHT_LEVEL, FULL_FRAME_SHAPE, write_synth_raw

# A full-frame-ish synthetic linear image (5D Mark IV is 6720x4480).
_SYNTHETIC = (
    np.random.default_rng(0).random((4480, 6720, 3), dtype=np.float64) * 65535.0
)


@pytest.fixture(scope="session")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A full-resolution raw frame, built once per session (~60 MB on disk)."""
    raw_dir = tmp_path_factory.mktemp("bench_raw")
    return write_synth_raw(
        raw_dir / "bench.cr2", BRIGHT_LEVEL, shape=FULL_FRAME_SHAPE
    )


def test_bench_analyze_rgb_synthetic(benchmark) -> None:
    benchmark(analyze_rgb, _SYNTHETIC)


def test_bench_decode_half_size(benchmark, sample: Path) -> None:
    benchmark(decode_linear_rgb, sample, half_size=True)


def test_bench_decode_full_size(benchmark, sample: Path) -> None:
    benchmark(decode_linear_rgb, sample, half_size=False)


def test_bench_analyze_cr2_half_size(benchmark, sample: Path) -> None:
    benchmark(analyze_cr2, sample, half_size=True)


def test_bench_ramping_decide(benchmark) -> None:
    engine = RampingEngine(RampParams(), LocationConfig())
    state = ExposureState(iso="800", aperture="8", shutter="1/125")
    metrics = analyze_rgb(np.full((64, 64, 3), 20000.0))
    now = datetime(2024, 6, 21, 18, 0)
    benchmark(engine.decide, state, metrics, now)
