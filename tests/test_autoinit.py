# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the auto-init ETTR search, with a mock camera + fake decoder."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from solramp import autoinit
from solramp.camera_tables import APERTURE_AXIS
from solramp.config import build_config
from solramp.ramping import ExposureState

from .conftest import FakeCamera, make_metrics


def test_auto_initialize_ramps_up_and_cleans_up(
    tmp_path: Path, fake_camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(
        {
            "interval_seconds": 30,
            "output_dir": str(tmp_path / "run"),
            "create_timestamped_subdirectory": False,
            "auto_init_keep_files": False,
        },
        now=datetime(2024, 6, 21, 18, 0, 0),
    )
    params = config.ramp
    dark_start = ExposureState(
        params.iso_dark_limit, params.aperture_dark_limit, params.shutter_dark_limit
    )
    start_stops = dark_start.total_stops()

    calls = {"n": 0}

    def fake_analyze(path, **_kwargs):
        # Brightness rises one stop per stop of light added from the dark start.
        calls["n"] += 1
        delta = fake_camera.last_state.total_stops() - start_stops
        highlight_ev = -4.0 + delta
        white = 100.0 if highlight_ev > -0.1 else 0.0
        return make_metrics(highlight_ev=highlight_ev, white=white)

    monkeypatch.setattr(autoinit, "analyze_cr2", fake_analyze)

    final = autoinit.auto_initialize(fake_camera, config, APERTURE_AXIS)

    # Converged to a brighter exposure than the (darkest) start point.
    assert final.total_stops() > start_stops
    assert calls["n"] >= 2
    assert len(fake_camera.captures) >= 2
    # Temporary test frames are removed when keep-files is disabled.
    assert not (config.output_dir / ".autoinit").exists()


def test_auto_initialize_keeps_files_by_default(
    tmp_path: Path, fake_camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(
        {
            "interval_seconds": 30,
            "output_dir": str(tmp_path / "run"),
            "create_timestamped_subdirectory": False,
        },
        now=datetime(2024, 6, 21, 18, 0, 0),
    )
    assert config.auto_init_keep_files is True
    monkeypatch.setattr(
        autoinit,
        "analyze_cr2",
        lambda path, **_k: make_metrics(highlight_ev=-0.05, white=0.0),
    )
    autoinit.auto_initialize(fake_camera, config, APERTURE_AXIS)
    # Test frames are retained for review.
    assert (config.output_dir / ".autoinit").exists()


def test_auto_initialize_hands_off_each_frame_after_analysis(
    tmp_path: Path, fake_camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(
        {
            "interval_seconds": 30,
            "output_dir": str(tmp_path / "run"),
            "create_timestamped_subdirectory": False,
        },
        now=datetime(2024, 6, 21, 18, 0, 0),
    )
    events: list[str] = []

    def fake_analyze(path, **_kwargs):
        events.append(f"analyse {path.name}")
        # Dark first, on target second.
        hi = -0.05 if len(events) > 2 else -3.0
        return make_metrics(highlight_ev=hi, white=0.0)

    def on_frame(path: Path) -> None:
        assert path.is_file()
        events.append(f"ship {path.name}")

    monkeypatch.setattr(autoinit, "analyze_cr2", fake_analyze)
    autoinit.auto_initialize(
        fake_camera,
        config,
        APERTURE_AXIS,
        on_frame=on_frame,
        on_finish=lambda: events.append("finish"),
    )

    assert events[:2] == ["analyse init_00.cr2", "ship init_00.cr2"]
    assert events[2:4] == ["analyse init_01.cr2", "ship init_01.cr2"]
    assert events[-1] == "finish"


def test_auto_initialize_removes_the_dir_once_every_frame_was_shipped(
    tmp_path: Path, fake_camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(
        {
            "interval_seconds": 30,
            "output_dir": str(tmp_path / "run"),
            "create_timestamped_subdirectory": False,
        },
        now=datetime(2024, 6, 21, 18, 0, 0),
    )
    monkeypatch.setattr(
        autoinit,
        "analyze_cr2",
        lambda path, **_k: make_metrics(highlight_ev=-0.05, white=0.0),
    )
    # Stand-in for rsync --remove-source-files.
    autoinit.auto_initialize(
        fake_camera, config, APERTURE_AXIS, on_frame=lambda p: p.unlink()
    )
    assert not (config.output_dir / ".autoinit").exists()


def test_auto_initialize_backs_off_on_white_clip(
    tmp_path: Path, fake_camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(
        {
            "interval_seconds": 30,
            "output_dir": str(tmp_path / "run"),
            "create_timestamped_subdirectory": False,
            "auto_init_keep_files": False,
        },
        now=datetime(2024, 6, 21, 18, 0, 0),
    )

    # Always report blown highlights -> engine must immediately back off + stop.
    monkeypatch.setattr(
        autoinit,
        "analyze_cr2",
        lambda path, **_k: make_metrics(highlight_ev=0.0, white=50.0),
    )

    final = autoinit.auto_initialize(fake_camera, config, APERTURE_AXIS)
    assert isinstance(final, ExposureState)
    # One capture, then a single back-off step.
    assert len(fake_camera.captures) == 1
    assert not (config.output_dir / ".autoinit").exists()
