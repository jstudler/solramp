# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the session orchestration in :func:`solramp.app.run`."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from solramp import app
from solramp.config import build_config
from solramp.logging_setup import configure_logging

needs_rsync = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync is not installed"
)


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


@needs_rsync
def test_crashing_session_still_ships_its_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    config = build_config(
        {
            "interval_seconds": 30,
            "output_dir": str(tmp_path / "capture"),
            "rsync_dest": str(remote),
            "visualizer": False,
        },
        now=datetime(2024, 6, 21, 18, 0, 0),
    )
    configure_logging(
        "WARNING",
        log_file=config.log_file_path,
        rotate_seconds=config.log_rotate_seconds,
    )

    def explode(_config: object) -> None:
        raise RuntimeError("boom from the camera layer")

    monkeypatch.setattr(app, "create_camera", explode)

    with pytest.raises(RuntimeError, match="boom"):
        app.run(config)

    shipped = "".join(
        path.read_text(encoding="utf-8")
        for path in (remote / config.output_dir.name).glob("solramp_*.log")
    )
    assert "Session crashed" in shipped
    assert "Traceback" in shipped
    assert "boom from the camera layer" in shipped
