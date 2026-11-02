# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pytest fixtures and helpers for the solramp test-suite.

Raw fixtures are *generated* by :mod:`tests.synth_raw` into a temporary
directory -- tiny uncompressed DNGs that libraw really decodes, so the analyser
is exercised end-to-end without committing ~31 MB camera files.  Everything
else is built from synthetic numpy arrays or mock cameras so the unit tests
need no hardware.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from solramp.camera import CapturedFrame
from solramp.exposure import FrameMetrics
from solramp.ramping import ExposureState

from .synth_raw import (
    BRIGHT_LEVEL,
    CLIPPED_LEVEL,
    CRUSHED_LEVEL,
    CRUSHED_SPREAD,
    DARK_LEVEL,
    write_synth_raw,
)

WHITE_LEVEL = 65535.0


# --------------------------------------------------------------------------
# Synthetic image helpers
# --------------------------------------------------------------------------
def solid_rgb(value: float, *, shape: tuple[int, int] = (16, 16)) -> np.ndarray:
    """A uniform ``H x W x 3`` linear-light frame at ``value`` on every channel."""
    return np.full((*shape, 3), value, dtype=np.float64)


def gradient_rgb(
    low: float, high: float, *, shape: tuple[int, int] = (16, 16)
) -> np.ndarray:
    """A horizontal luminance gradient from ``low`` to ``high``."""
    row = np.linspace(low, high, shape[1], dtype=np.float64)
    plane = np.tile(row, (shape[0], 1))
    return np.stack([plane, plane, plane], axis=2)


@pytest.fixture
def white_level() -> float:
    return WHITE_LEVEL


# --------------------------------------------------------------------------
# Generated raw fixtures
#
# Written with a ``.cr2`` suffix so paths look like real captures; libraw
# identifies formats by content, not by extension.  Session-scoped because the
# files are immutable and decoding them is the slow part.
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def raw_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("raw_fixtures")


@pytest.fixture(scope="session")
def sample_cr2_bright(raw_dir: Path) -> Path:
    """A well-exposed frame, roughly -0.71 EV below saturation."""
    return write_synth_raw(raw_dir / "bright.cr2", BRIGHT_LEVEL)


@pytest.fixture(scope="session")
def sample_cr2_dark(raw_dir: Path) -> Path:
    """A late-dusk frame, roughly -5.77 EV below saturation."""
    return write_synth_raw(raw_dir / "dark.cr2", DARK_LEVEL)


@pytest.fixture(scope="session")
def sample_cr2_clipped(raw_dir: Path) -> Path:
    """A blown-out frame: every pixel above the white-clipping threshold."""
    return write_synth_raw(raw_dir / "clipped.cr2", CLIPPED_LEVEL)


@pytest.fixture(scope="session")
def sample_cr2_crushed(raw_dir: Path) -> Path:
    """An underexposed frame: every pixel below the black-clipping threshold."""
    return write_synth_raw(
        raw_dir / "crushed.cr2", CRUSHED_LEVEL, spread=CRUSHED_SPREAD
    )


@pytest.fixture(scope="session")
def sample_cr2(sample_cr2_bright: Path) -> Path:
    """A single representative raw frame (the bright one)."""
    return sample_cr2_bright


# --------------------------------------------------------------------------
# Metrics + exposure-state helpers
# --------------------------------------------------------------------------
def make_metrics(
    *,
    highlight_ev: float = -1.0,
    white: float = 0.0,
    black: float = 0.0,
    mean_ev: float | None = None,
    saturation: float = 0.1,
) -> FrameMetrics:
    """Build a :class:`FrameMetrics` with explicit control signals for ramping."""
    return FrameMetrics(
        clipping_white_pct=white,
        clipping_black_pct=black,
        mean_ev=highlight_ev - 2.0 if mean_ev is None else mean_ev,
        highlight_ev=highlight_ev,
        mean_saturation=saturation,
    )


@pytest.fixture
def darkest_state() -> ExposureState:
    """The maximally-underexposed state used as the auto-init start point."""
    return ExposureState(iso="100", aperture="22", shutter="1/8000")


# --------------------------------------------------------------------------
# Mock camera
# --------------------------------------------------------------------------
@dataclass
class _AppliedCall:
    target: ExposureState
    current: ExposureState | None


class FakeCamera:
    """In-memory stand-in for :class:`solramp.camera.Camera`.

    Records every settings change, fabricates :class:`CapturedFrame` objects and
    writes tiny placeholder files on download so the pipeline/auto-init logic can
    be exercised without hardware.

    ``capture_side_effects`` and ``download_side_effects`` are consumed in order:
    each entry is either an exception to raise or ``None`` to succeed normally.
    """

    def __init__(self, *, start: ExposureState | None = None) -> None:
        self.applied: list[_AppliedCall] = []
        self.captures: list[ExposureState] = []
        self.downloads: list[Path] = []
        self.reconnect_calls: int = 0
        self.last_state: ExposureState = start or ExposureState("100", "22", "1/8000")
        self._counter = 0
        self.capture_side_effects: list[Exception | None] = []
        self.download_side_effects: list[Exception | None] = []

    def apply_exposure_state(
        self, target: ExposureState, *, current: ExposureState | None = None
    ) -> None:
        self.applied.append(_AppliedCall(target, current))
        self.last_state = target

    def read_exposure_state(self) -> ExposureState:
        return self.last_state

    def reconnect(self) -> None:
        self.reconnect_calls += 1

    def capture(self, settings: ExposureState) -> CapturedFrame:
        if self.capture_side_effects:
            effect = self.capture_side_effects.pop(0)
            if effect is not None:
                raise effect
        self.captures.append(settings)
        self.last_state = settings
        self._counter += 1
        return CapturedFrame(
            camera_folder="/store_00010001/DCIM/100CANON",
            camera_name=f"IMG_{self._counter:04d}.CR2",
            capture_time=time.time(),
            settings=settings,
        )

    def download(self, frame: CapturedFrame, dest_path: Path) -> Path:
        if self.download_side_effects:
            effect = self.download_side_effects.pop(0)
            if effect is not None:
                raise effect
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"\x00CR2-placeholder")
        self.downloads.append(dest_path)
        return dest_path


@pytest.fixture
def fake_camera() -> FakeCamera:
    return FakeCamera()
