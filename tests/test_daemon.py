# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the daemon supervisor.

The real :func:`solramp.app.run` and camera are replaced with fakes so the
session loop, the fresh-folder-per-session rule and the backoff ladder are
deterministic and run instantly.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from solramp import daemon
from solramp.app import EXIT_CAMERA_LOST, EXIT_FATAL, EXIT_NOT_READY, EXIT_OK
from solramp.config import Config


class FakeTransport:
    """Reachable only after ``reachable_after`` polls."""

    def __init__(self, *, reachable_after: int = 0) -> None:
        self.reachable_after = reachable_after
        self.checks = 0

    def describe(self) -> str:
        return "fake:camera"

    def is_reachable(self, timeout: float = 3.0) -> bool:
        self.checks += 1
        return self.checks > self.reachable_after

    def wait_for_arrival(self, stop: threading.Event, *, poll: float = 15.0) -> bool:
        while not self.is_reachable():
            if stop.wait(0):  # no real sleeping in tests
                return False
        return True


class FakeCameraHandle:
    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport


class FakeVisualizer:
    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


@pytest.fixture(autouse=True)
def no_real_visualizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the supervisor from binding a real port in every test."""
    monkeypatch.setattr(daemon, "start_visualizer", lambda config: None)


@pytest.fixture
def overrides(tmp_path: Path) -> dict[str, Any]:
    return {
        "interval_seconds": 20,
        "output_dir": str(tmp_path / "captures"),
        "daemon": True,
        "daemon_poll_seconds": 0.01,
        "camera_address": "10.0.0.1",
    }


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    transport: FakeTransport,
    codes: list[int],
) -> list[Config]:
    """Drive the supervisor through ``codes``, then stop it. Returns configs seen."""
    seen: list[Config] = []

    monkeypatch.setattr(
        daemon, "create_camera", lambda config: FakeCameraHandle(transport)
    )
    # Real sleeps would make these tests take minutes.
    monkeypatch.setattr(daemon, "_BACKOFF_SECONDS", (0.0,))

    def fake_run(config: Config, *, stop_event: threading.Event) -> int:
        seen.append(config)
        if not codes:
            stop_event.set()
            return EXIT_OK
        return codes.pop(0)

    monkeypatch.setattr(daemon, "run", fake_run)
    return seen


def test_waits_for_camera_then_runs_a_session(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    transport = FakeTransport(reachable_after=3)
    seen = _patch(monkeypatch, transport, [])

    assert daemon.supervise(overrides, None) == EXIT_OK
    assert len(seen) == 1
    assert transport.checks > 3


def test_each_session_gets_a_new_output_folder(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    transport = FakeTransport()
    seen = _patch(monkeypatch, transport, [EXIT_CAMERA_LOST, EXIT_CAMERA_LOST])

    times = iter(
        [
            datetime(2024, 6, 21, 18, 0, 0),
            datetime(2024, 6, 21, 19, 30, 0),
            datetime(2024, 6, 21, 21, 15, 0),
        ]
    )
    monkeypatch.setattr(daemon, "datetime", _FrozenDatetime(times))

    daemon.supervise(overrides, None)

    folders = [config.output_dir for config in seen]
    assert len(folders) == 3
    assert len(set(folders)) == 3
    assert folders[0].name == "2024-06-21-18-00-00"
    assert folders[2].name == "2024-06-21-21-15-00"


def test_fatal_exit_stops_the_daemon(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    transport = FakeTransport()
    seen = _patch(monkeypatch, transport, [EXIT_FATAL, EXIT_CAMERA_LOST])

    assert daemon.supervise(overrides, None) == EXIT_FATAL
    # Gave up after the fatal session instead of starting another.
    assert len(seen) == 1


def test_stop_during_a_session_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    transport = FakeTransport()
    monkeypatch.setattr(
        daemon, "create_camera", lambda config: FakeCameraHandle(transport)
    )

    def fake_run(config: Config, *, stop_event: threading.Event) -> int:
        stop_event.set()
        return EXIT_OK

    monkeypatch.setattr(daemon, "run", fake_run)

    assert daemon.supervise(overrides, None) == EXIT_OK


def test_crashing_session_does_not_kill_the_daemon(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    transport = FakeTransport()
    monkeypatch.setattr(
        daemon, "create_camera", lambda config: FakeCameraHandle(transport)
    )
    monkeypatch.setattr(daemon, "_BACKOFF_SECONDS", (0.0,))
    calls: list[int] = []

    def fake_run(config: Config, *, stop_event: threading.Event) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        stop_event.set()
        return EXIT_OK

    monkeypatch.setattr(daemon, "run", fake_run)

    assert daemon.supervise(overrides, None) == EXIT_OK
    assert len(calls) == 2


def test_session_log_file_is_closed_between_sessions(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    transport = FakeTransport()
    levels: list[Path | None] = []
    _patch(monkeypatch, transport, [EXIT_CAMERA_LOST])

    real_configure = daemon.configure_logging

    def spy(stdout_level: str, *, log_file: Path | None = None, **kw: Any) -> None:
        levels.append(log_file)
        real_configure(stdout_level, log_file=log_file, **kw)

    monkeypatch.setattr(daemon, "configure_logging", spy)
    daemon.supervise(overrides, None)

    # Each session: point at its log file, then back to stdout-only.
    assert levels[0] is not None
    assert levels[1] is None


def test_one_visualizer_serves_every_session(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    transport = FakeTransport()
    seen = _patch(monkeypatch, transport, [EXIT_CAMERA_LOST, EXIT_CAMERA_LOST])
    started: list[FakeVisualizer] = []

    def fake_start(config: Config) -> FakeVisualizer:
        started.append(FakeVisualizer())
        return started[-1]

    monkeypatch.setattr(daemon, "start_visualizer", fake_start)
    daemon.supervise(overrides, None)

    assert len(seen) == 3
    # Rebinding the port per session would race the previous listener.
    assert len(started) == 1
    assert started[0].stopped == 1


def test_visualizer_is_stopped_when_a_session_is_fatal(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    transport = FakeTransport()
    _patch(monkeypatch, transport, [EXIT_FATAL])
    visualizer = FakeVisualizer()
    monkeypatch.setattr(daemon, "start_visualizer", lambda config: visualizer)

    assert daemon.supervise(overrides, None) == EXIT_FATAL
    assert visualizer.stopped == 1


def test_no_visualizer_when_disabled(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    transport = FakeTransport()
    _patch(monkeypatch, transport, [])
    calls: list[Config] = []
    monkeypatch.setattr(daemon, "start_visualizer", lambda config: calls.append(config))

    daemon.supervise({**overrides, "visualizer": False}, None)

    assert calls == []


# --------------------------------------------------------------------------
# Backoff ladder
# --------------------------------------------------------------------------
def _config(**kw: Any) -> Config:
    from solramp.config import build_config

    return build_config({"interval_seconds": 20, **kw}, now=datetime(2024, 1, 1))


def test_backoff_escalates_while_sessions_keep_failing() -> None:
    base = _config()
    step = 0
    for expected in (1, 2, 3):
        step = daemon._next_backoff_step(base, EXIT_NOT_READY, 0.5, step)
        assert step == expected


def test_backoff_resets_after_a_productive_session() -> None:
    base = _config()
    # Longer than 2 x interval -> the camera really did work for a while.
    assert daemon._next_backoff_step(base, EXIT_CAMERA_LOST, 300.0, 3) == 0


def test_backoff_escalates_on_an_immediately_lost_session() -> None:
    base = _config()
    assert daemon._next_backoff_step(base, EXIT_CAMERA_LOST, 1.0, 1) == 2


def test_backoff_is_capped() -> None:
    assert daemon._backoff(99) == daemon._BACKOFF_SECONDS[-1]


class _FrozenDatetime:
    """Hands out a scripted sequence of ``datetime.now()`` values."""

    def __init__(self, values: Any) -> None:
        self._values = values

    def now(self, tz: Any = None) -> datetime:
        return next(self._values)
