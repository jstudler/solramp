# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pipeline's ramp-application rules and lag compensation.

These exercise the scheduler logic directly (no threads, no real camera) so the
"at least two identical frames in a row" rule and the download-lag correction
are deterministic.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest

import gphoto2 as gp

from solramp.astral_phase import Phase
from solramp.camera import CapturedFrame
from solramp.config import build_config
from solramp.metadata import MetadataStore
from solramp.pipeline import AnalysisJob, Pipeline
from solramp.ramping import ExposureState, RampDecision, RampingEngine

from .conftest import FakeCamera, make_metrics

STATE_A = ExposureState(iso="800", aperture="8", shutter="1/125")
STATE_B = STATE_A.with_value("aperture", "7.1")
SESSION = {"started_at": "2024-06-21T18:00:00"}


@pytest.fixture
def pipeline(tmp_path: Path, fake_camera: FakeCamera) -> Pipeline:
    config = build_config(
        {"interval_seconds": 20, "output_dir": str(tmp_path / "run")},
        now=datetime(2024, 6, 21, 18, 0, 0),
    )
    engine = RampingEngine(config.ramp, config.location)
    store, _ = MetadataStore.load_or_create(config.metadata_path, session=SESSION)
    return Pipeline(config, fake_camera, engine, store)


# --------------------------------------------------------------------------
# Two-identical-frames rule
# --------------------------------------------------------------------------
def test_pending_not_applied_until_two_frames_held(
    pipeline: Pipeline, fake_camera: FakeCamera
) -> None:
    pipeline._current_settings = STATE_A
    pipeline._frames_since_change = 0
    pipeline.set_pending(STATE_B, "open up")

    pipeline._apply_pending()
    # Only one frame at the current settings so far -> must wait.
    assert pipeline._current_settings == STATE_A
    assert pipeline._pending is not None
    assert fake_camera.applied == []

    pipeline._frames_since_change = 2
    pipeline._apply_pending()
    assert pipeline._current_settings == STATE_B
    assert pipeline._pending is None
    assert pipeline._frames_since_change == 0
    assert len(fake_camera.applied) == 1
    assert fake_camera.applied[0].target == STATE_B
    assert fake_camera.applied[0].current == STATE_A


def test_pending_applied_even_when_behind_schedule(
    pipeline: Pipeline, fake_camera: FakeCamera
) -> None:
    # Regression: a long-exposure cycle that overruns the interval used to
    # starve the ramp forever, freezing the shutter and letting the night go
    # black. The ramp must now apply regardless of timing.
    pipeline._current_settings = STATE_A
    pipeline._frames_since_change = 5
    pipeline.set_pending(STATE_B, "open up")
    pipeline._apply_pending()
    assert pipeline._current_settings == STATE_B
    assert pipeline._pending is None
    assert len(fake_camera.applied) == 1
    assert fake_camera.applied[0].target == STATE_B


def test_pending_equal_to_current_is_cleared(
    pipeline: Pipeline, fake_camera: FakeCamera
) -> None:
    pipeline._current_settings = STATE_A
    pipeline._frames_since_change = 5
    pipeline.set_pending(STATE_A, "noop")
    pipeline._apply_pending()
    assert pipeline._pending is None
    assert fake_camera.applied == []


# --------------------------------------------------------------------------
# De-duplication + dest path
# --------------------------------------------------------------------------
def test_enqueue_existing_file_dedups(pipeline: Pipeline, tmp_path: Path) -> None:
    cr2 = pipeline.config.output_dir / "already.cr2"
    cr2.parent.mkdir(parents=True, exist_ok=True)
    cr2.write_bytes(b"x")
    pipeline.enqueue_existing_file(cr2)
    pipeline.enqueue_existing_file(cr2)
    assert pipeline._analysis_queue.qsize() == 1


def test_dest_path_uses_capture_timestamp(pipeline: Pipeline) -> None:
    ts = datetime(2024, 6, 21, 18, 30, 5).timestamp()
    frame = CapturedFrame(
        camera_folder="/store", camera_name="IMG_0001.CR2", capture_time=ts, settings=STATE_A
    )
    dest = pipeline._dest_path_for(frame)
    assert dest.name == "2024-06-21_18-30-05.cr2"
    assert dest.parent == pipeline.config.output_dir


# --------------------------------------------------------------------------
# Download-lag compensation
# --------------------------------------------------------------------------
def test_decide_live_compensates_for_settings_lag(pipeline: Pipeline) -> None:
    # The frame was shot at STATE_A, but the camera has since ramped +1 stop.
    current = STATE_A.with_value("iso", "1600")  # 800 -> 1600 == +1 stop
    pipeline._current_settings = current
    pipeline._last_analysed_capture_time = 0.0

    captured: dict[str, object] = {}

    class _RecordingEngine:
        params = pipeline.engine.params

        def decide(self, state, metrics, now):
            captured["state"] = state
            captured["metrics"] = metrics
            return RampDecision.no_change(state, 0.0, Phase.DARKENING, "test")

    pipeline.engine = _RecordingEngine()
    job = AnalysisJob(path=Path("x.cr2"), capture_time=10.0, settings=STATE_A, live=True)

    pipeline._decide_live(job, make_metrics(highlight_ev=-3.0), datetime(2024, 6, 21, 18, 0))

    lag = current.total_stops() - STATE_A.total_stops()
    assert lag == pytest.approx(1.0)
    # The metered EV is shifted by the lag so the engine sees the *current* scene.
    assert captured["metrics"].highlight_ev == pytest.approx(-3.0 + lag)
    assert captured["state"] == current


def test_decide_live_accepts_lagged_frame_when_cycle_overruns(
    pipeline: Pipeline,
) -> None:
    """Regression: when capture cycle > interval, the analysis thread always
    lags behind. The gate must still accept decisions from older (lagged)
    frames, as long as they are the newest *analysed* one."""
    from solramp.ramping import LightDirection, RampDecision

    pipeline._current_settings = STATE_A
    pipeline._last_analysed_capture_time = 0.0
    # Simulate: capture thread already moved _last_live_capture_time ahead.
    pipeline._last_live_capture_time = 100.0

    class _AlwaysRemoveEngine:
        params = pipeline.engine.params

        def decide(self, state, metrics, now):
            new = state.with_value("iso", "400")
            return RampDecision(
                changed=True,
                old_state=state,
                new_state=new,
                error_ev=-1.0,
                phase=Phase.BRIGHTENING,
                reason="test",
                param="iso",
                direction=LightDirection.REMOVE,
                steps=1,
                applied_stops=0.33,
            )

    pipeline.engine = _AlwaysRemoveEngine()
    # Frame captured at t=50 — newer than last *analysed* (0) but older than
    # last *captured* (100). Old gate would reject it; new gate accepts it.
    job = AnalysisJob(path=Path("x.cr2"), capture_time=50.0, settings=STATE_A, live=True)
    pipeline._decide_live(job, make_metrics(highlight_ev=-3.0), datetime(2024, 6, 21, 5, 0))

    assert pipeline._pending is not None
    target, _ = pipeline._pending
    assert target.iso == "400"
    assert pipeline._last_analysed_capture_time == 50.0


# --------------------------------------------------------------------------
# Camera reconnect on I/O errors
# --------------------------------------------------------------------------
def test_capture_reconnects_on_io_in_progress(
    pipeline: Pipeline, fake_camera: FakeCamera
) -> None:
    pipeline._current_settings = STATE_A
    # First attempt raises -110; second succeeds.
    fake_camera.capture_side_effects = [gp.GPhoto2Error(-110), None]

    frame = pipeline._capture_with_retries(STATE_A)

    assert frame is not None
    assert fake_camera.reconnect_calls == 1
    assert len(fake_camera.captures) == 1


def test_capture_exhausts_retries_without_reconnect_for_other_errors(
    pipeline: Pipeline, fake_camera: FakeCamera
) -> None:
    # Error -1 is not an I/O port error — should NOT trigger reconnect.
    fake_camera.capture_side_effects = [
        gp.GPhoto2Error(-1)
    ] * pipeline.config.capture_retries

    frame = pipeline._capture_with_retries(STATE_A)

    assert frame is None
    assert fake_camera.reconnect_calls == 0


def test_download_drops_frame_immediately_on_file_not_found(
    pipeline: Pipeline, fake_camera: FakeCamera, tmp_path: Path
) -> None:
    frame = fake_camera.capture(STATE_A)
    dest = tmp_path / "lost.cr2"
    fake_camera.download_side_effects = [gp.GPhoto2Error(-6)]

    result = pipeline._download_with_retries(frame, dest)

    assert result is None
    # Only one attempt — should not burn remaining retries.
    assert len(fake_camera.downloads) == 0
    assert fake_camera.reconnect_calls == 0


def test_download_reconnects_on_io_error(
    pipeline: Pipeline, fake_camera: FakeCamera, tmp_path: Path
) -> None:
    frame = fake_camera.capture(STATE_A)
    dest = tmp_path / "frame.cr2"
    fake_camera.download_side_effects = [gp.GPhoto2Error(-110), None]

    result = pipeline._download_with_retries(frame, dest)

    assert result is not None and result >= 0.0
    assert fake_camera.reconnect_calls == 1


def test_download_gives_up_immediately_once_stopped(
    pipeline: Pipeline, fake_camera: FakeCamera, tmp_path: Path
) -> None:
    frame = fake_camera.capture(STATE_A)
    pipeline._stop.set()

    result = pipeline._download_with_retries(frame, tmp_path / "frame.cr2")

    assert result is None
    assert fake_camera.downloads == []
    assert fake_camera.reconnect_calls == 0


# --------------------------------------------------------------------------
# Camera-loss detection (ends the session so the daemon starts a new folder)
# --------------------------------------------------------------------------
def test_lost_after_is_cycles_times_interval(pipeline: Pipeline) -> None:
    # Fixture config: interval 20s, default 5 cycles.
    assert pipeline._lost_after == pytest.approx(100.0)


def test_camera_not_lost_before_threshold(pipeline: Pipeline) -> None:
    pipeline._last_success = time.monotonic() - 99.0

    pipeline._check_camera_lost()

    assert not pipeline.camera_lost.is_set()
    assert not pipeline._stop.is_set()


def test_camera_lost_after_threshold(pipeline: Pipeline) -> None:
    pipeline._last_success = time.monotonic() - 101.0

    pipeline._check_camera_lost()

    assert pipeline.camera_lost.is_set()
    assert pipeline._stop.is_set()


def test_camera_lost_threshold_honours_config(
    tmp_path: Path, fake_camera: FakeCamera
) -> None:
    config = build_config(
        {
            "interval_seconds": 20,
            "output_dir": str(tmp_path / "run"),
            "camera_lost_cycles": 2,
        },
        now=datetime(2024, 6, 21, 18, 0, 0),
    )
    engine = RampingEngine(config.ramp, config.location)
    store, _ = MetadataStore.load_or_create(config.metadata_path, session=SESSION)
    pipeline = Pipeline(config, fake_camera, engine, store)
    assert pipeline._lost_after == pytest.approx(40.0)

    pipeline._last_success = time.monotonic() - 41.0
    pipeline._check_camera_lost()

    assert pipeline.camera_lost.is_set()


# --------------------------------------------------------------------------
# Capture cadence
# --------------------------------------------------------------------------
def test_overrun_drifts_instead_of_bursting_to_catch_up(
    pipeline: Pipeline, fake_camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: cycles slower than the interval (long exposure + LENR) used to
    # build up a backlog that was later fired off back-to-back once captures
    # became fast again (a frame every ~2s at dawn).
    clock = [1000.0]
    durations = [35.0] * 5 + [2.0] * 5
    shots: list[float] = []

    def fake_capture(settings: ExposureState) -> CapturedFrame:
        shots.append(clock[0])
        clock[0] += durations[len(shots) - 1]
        if len(shots) == len(durations):
            pipeline._stop.set()
        return fake_camera.capture(settings)

    def fake_sleep_until(target: float) -> None:
        clock[0] = max(clock[0], target)

    pipeline._current_settings = STATE_A
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pipeline, "_capture_with_retries", fake_capture)
    monkeypatch.setattr(pipeline, "_sleep_until", fake_sleep_until)

    pipeline._capture_loop()

    gaps = [b - a for a, b in zip(shots, shots[1:], strict=False)]
    assert gaps == pytest.approx([35.0] * 5 + [20.0] * 4)


# --------------------------------------------------------------------------
# Hand-off to the remote syncer
# --------------------------------------------------------------------------
class _FakeSyncer:
    def __init__(self) -> None:
        self.enqueued: list[Path] = []

    def enqueue(self, path: Path) -> None:
        self.enqueued.append(path)


@pytest.fixture
def syncing(tmp_path: Path, fake_camera: FakeCamera) -> tuple[Pipeline, _FakeSyncer]:
    config = build_config(
        {"interval_seconds": 20, "output_dir": str(tmp_path / "run")},
        now=datetime(2024, 6, 21, 18, 0, 0),
    )
    engine = RampingEngine(config.ramp, config.location)
    store, _ = MetadataStore.load_or_create(config.metadata_path, session=SESSION)
    syncer = _FakeSyncer()
    return Pipeline(config, fake_camera, engine, store, syncer), syncer


def _drain_analysis(pipeline: Pipeline, *jobs: AnalysisJob) -> None:
    for job in jobs:
        pipeline._analysis_queue.put(job)
    pipeline._stop.set()
    pipeline._analyse_loop()


def _job(path: Path) -> AnalysisJob:
    return AnalysisJob(path=path, capture_time=time.time(), settings=STATE_A, live=False)


def test_analysed_frame_is_handed_to_the_syncer(
    syncing: tuple[Pipeline, _FakeSyncer], sample_cr2: Path, tmp_path: Path
) -> None:
    pipeline, syncer = syncing
    frame = tmp_path / "run" / "2024-06-21_18-00-10.cr2"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(sample_cr2.read_bytes())

    _drain_analysis(pipeline, _job(frame))

    assert syncer.enqueued == [frame]
    assert pipeline.metadata.has_frame(frame.name)


def test_undecodable_frame_is_still_handed_over(
    syncing: tuple[Pipeline, _FakeSyncer], tmp_path: Path
) -> None:
    # Otherwise a corrupt CR2 would pin ~35 MB of RAM for the whole session.
    pipeline, syncer = syncing
    broken = tmp_path / "run" / "broken.cr2"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not a raw file")

    _drain_analysis(pipeline, _job(broken))

    assert syncer.enqueued == [broken]
    assert not pipeline.metadata.has_frame(broken.name)


def test_already_analysed_frame_is_still_handed_over(
    syncing: tuple[Pipeline, _FakeSyncer], sample_cr2: Path, tmp_path: Path
) -> None:
    pipeline, syncer = syncing
    frame = tmp_path / "run" / "2024-06-21_18-00-10.cr2"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(sample_cr2.read_bytes())

    _drain_analysis(pipeline, _job(frame), _job(frame))

    # Analysed once, but freed on both passes.
    assert syncer.enqueued == [frame, frame]
    assert pipeline.metadata.frame_count == 1
