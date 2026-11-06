# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the metadata dashboard: discovery, HTTP routes and lifecycle."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
import yaml

from solramp.config import METADATA_FILENAME, build_config
from solramp.visualizer import (
    _PARSE_CACHE_SIZE,
    SessionRegistry,
    VisualizerServer,
    _capture_time,
    start_visualizer,
)


def _write_session(folder: Path, *, frames: int = 2, name: str = "Test site") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / METADATA_FILENAME
    path.write_text(
        yaml.safe_dump(
            {
                "session": {"started_at": "2024-06-21T18:00:00", "location": {"name": name}},
                "frames": [
                    {
                        "filename": f"2024-06-21_18-00-{i:02d}.cr2",
                        # Analysis always finishes well after the shutter.
                        "timestamp_analysis": f"2024-06-21T18:00:{i + 30:02d}+02:00",
                        "iso": 100,
                        "aperture": 5.6,
                        "shutter_seconds": 0.5,
                        "clipping_white_pct": 0.1,
                        "phase": "darkening",
                    }
                    for i in range(frames)
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# SessionRegistry
# --------------------------------------------------------------------------- #
def test_discover_finds_sessions_recursively(tmp_path: Path) -> None:
    _write_session(tmp_path / "2024-06-21-18-00-00")
    _write_session(tmp_path / "nested" / "2024-06-22-18-00-00")

    found = SessionRegistry([tmp_path]).discover()

    assert set(found) == {"2024-06-21-18-00-00", "nested/2024-06-22-18-00-00"}


def test_single_root_identifier_is_relative_to_the_root(tmp_path: Path) -> None:
    _write_session(tmp_path)
    assert list(SessionRegistry([tmp_path]).discover()) == [tmp_path.name]


def test_multiple_roots_are_prefixed_and_collisions_disambiguated(tmp_path: Path) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    _write_session(left / "session-a")
    _write_session(right / "session-a")

    found = SessionRegistry([left, right]).discover()

    assert set(found) == {"left/session-a", "right/session-a"}


def test_missing_root_is_skipped(tmp_path: Path) -> None:
    _write_session(tmp_path / "here")
    registry = SessionRegistry([tmp_path, tmp_path / "does-not-exist"])
    assert list(registry.discover()) == [f"{tmp_path.name}/here"]


def test_list_sessions_is_newest_first(tmp_path: Path) -> None:
    old = _write_session(tmp_path / "old")
    new = _write_session(tmp_path / "new")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    assert [s["id"] for s in SessionRegistry([tmp_path]).list_sessions()] == ["new", "old"]


def test_load_session_normalises_frames(tmp_path: Path) -> None:
    _write_session(tmp_path / "s", frames=3)
    data = SessionRegistry([tmp_path]).load_session("s")

    assert data is not None
    assert len(data["frames"]) == 3
    assert data["session"]["location"]["name"] == "Test site"
    # Absent metrics normalise to None rather than blowing up the chart code.
    assert data["frames"][0]["mean_ev"] is None
    # The x-axis is the shutter time from the filename, not the analysis time.
    assert data["frames"][0]["t"] == "2024-06-21T18:00:00+02:00"


def test_capture_time_prefers_the_filename_and_keeps_the_recorded_offset() -> None:
    frame = {
        "filename": "2026-06-16_02-55-16.cr2",
        "timestamp_analysis": "2026-06-16T02:58:32+02:00",
    }
    assert _capture_time(frame) == "2026-06-16T02:55:16+02:00"


@pytest.mark.parametrize(
    "filename",
    ["IMG_0001.cr2", "", "2026-06-16.cr2"],
)
def test_capture_time_falls_back_when_the_filename_has_no_stamp(filename: str) -> None:
    analysis = "2026-06-16T02:58:32+02:00"
    frame = {"filename": filename, "timestamp_analysis": analysis}
    assert _capture_time(frame) == analysis


def test_capture_time_without_an_offset_stays_naive() -> None:
    frame = {"filename": "2026-06-16_02-55-16.cr2", "timestamp_analysis": ""}
    assert _capture_time(frame) == "2026-06-16T02:55:16"


def test_load_session_rejects_unknown_and_traversal_ids(tmp_path: Path) -> None:
    _write_session(tmp_path / "s")
    registry = SessionRegistry([tmp_path])

    assert registry.load_session("nope") is None
    assert registry.load_session("../../etc/passwd") is None
    assert registry.load_session("/etc/passwd") is None


def test_an_unchanged_sidecar_is_parsed_once(tmp_path: Path) -> None:
    _write_session(tmp_path / "s")
    registry = SessionRegistry([tmp_path])
    parses = 0
    real = yaml.load

    def counting_load(*args, **kwargs):
        nonlocal parses
        parses += 1
        return real(*args, **kwargs)

    with mock.patch.object(yaml, "load", counting_load):
        first = registry.load_session("s")
        second = registry.load_session("s")

    assert parses == 1
    assert first == second
    assert first["frames"]


def test_an_edited_sidecar_is_reread(tmp_path: Path) -> None:
    folder = tmp_path / "s"
    _write_session(folder, frames=2)
    registry = SessionRegistry([tmp_path])
    assert len(registry.load_session("s")["frames"]) == 2

    # Same path, new content: the cache keys on mtime+size, not the path alone.
    _write_session(folder, frames=5)
    os.utime(folder / METADATA_FILENAME, (0, 0))
    assert len(registry.load_session("s")["frames"]) == 5


def test_the_parse_cache_is_bounded(tmp_path: Path) -> None:
    for i in range(_PARSE_CACHE_SIZE + 3):
        _write_session(tmp_path / f"s{i}")
    registry = SessionRegistry([tmp_path])

    for i in range(_PARSE_CACHE_SIZE + 3):
        registry.load_session(f"s{i}")

    assert len(registry._cache) == _PARSE_CACHE_SIZE


def test_a_corrupt_sidecar_is_not_served(tmp_path: Path) -> None:
    folder = tmp_path / "s"
    folder.mkdir()
    (folder / METADATA_FILENAME).write_text("frames: [unclosed\n", encoding="utf-8")

    assert SessionRegistry([tmp_path]).load_session("s") is None


# --------------------------------------------------------------------------- #
# VisualizerServer
# --------------------------------------------------------------------------- #
@pytest.fixture
def served(tmp_path: Path):
    _write_session(tmp_path / "2024-06-21-18-00-00")
    server = VisualizerServer([tmp_path], port=0)
    assert server.start() is True
    try:
        yield server
    finally:
        server.stop()


def _get(server: VisualizerServer, route: str) -> tuple[int, bytes]:
    with urlopen(f"http://{server.host}:{server.port}{route}", timeout=5) as resp:
        return resp.status, resp.read()


def test_server_serves_the_dashboard_and_session_api(served: VisualizerServer) -> None:
    status, body = _get(served, "/")
    assert status == 200
    assert b"Timelapse Metadata Visualizer" in body

    status, body = _get(served, "/api/sessions")
    sessions = json.loads(body)["sessions"]
    assert [s["id"] for s in sessions] == ["2024-06-21-18-00-00"]

    status, body = _get(served, "/api/session?id=2024-06-21-18-00-00")
    assert len(json.loads(body)["frames"]) == 2


def test_server_404s_an_unknown_session(served: VisualizerServer) -> None:
    with pytest.raises(HTTPError) as excinfo:
        _get(served, "/api/session?id=nope")
    assert excinfo.value.code == 404


def test_new_sessions_appear_without_a_restart(served: VisualizerServer, tmp_path: Path) -> None:
    _write_session(tmp_path / "2024-06-22-18-00-00")
    _, body = _get(served, "/api/sessions")
    assert len(json.loads(body)["sessions"]) == 2


def test_stop_is_idempotent(tmp_path: Path) -> None:
    server = VisualizerServer([tmp_path], port=0)
    assert server.start() is True
    server.stop()
    server.stop()
    assert server.port is None


def test_start_on_a_taken_port_warns_instead_of_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        server = VisualizerServer([tmp_path], port=blocker.getsockname()[1])
        with caplog.at_level("WARNING"):
            assert server.start() is False
        assert "cannot bind" in caplog.text
    finally:
        blocker.close()


def test_non_loopback_host_logs_a_security_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server = VisualizerServer([tmp_path], host="0.0.0.0", port=0)  # noqa: S104
    with caplog.at_level("WARNING"):
        assert server.start() is True
    try:
        assert "no authentication" in caplog.text
    finally:
        server.stop()


def test_start_visualizer_uses_the_config_watch_folders(tmp_path: Path) -> None:
    _write_session(tmp_path / "captures" / "2024-06-21-18-00-00")
    config = build_config(
        {
            "visualizer_only": True,
            "output_dir": str(tmp_path / "captures"),
            "visualizer_port": 0,
            "visualizer_open_browser": False,
        }
    )
    server = start_visualizer(config)
    assert server is not None
    try:
        _, body = _get(server, "/api/sessions")
        assert [s["id"] for s in json.loads(body)["sessions"]] == ["2024-06-21-18-00-00"]
    finally:
        server.stop()
