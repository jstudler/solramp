# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the wall-clock log-file rotation."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from solramp.logging_setup import (
    TimeBucketFileHandler,
    bucket_start,
    configure_logging,
    log_bucket_path,
    rotated_log_files,
    split_log_files,
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


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        (600, datetime(2026, 10, 16, 10, 40)),
        (3600, datetime(2026, 10, 16, 10, 0)),
        (4 * 3600, datetime(2026, 10, 16, 8, 0)),
        (86400, datetime(2026, 10, 16, 0, 0)),
    ],
)
def test_bucket_start_aligns_to_the_local_wall_clock(
    interval: int, expected: datetime
) -> None:
    assert bucket_start(datetime(2026, 10, 16, 10, 47, 36), interval) == expected


def test_bucket_file_name_matches_session_folder_format(tmp_path: Path) -> None:
    path = log_bucket_path(tmp_path / "solramp.log", datetime(2026, 10, 16, 10, 0))
    assert path.name == "solramp_2026-10-16-10-00-00.log"


def _record(when: datetime, message: str) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
    record.created = when.timestamp()
    return record


def test_handler_starts_a_new_file_per_bucket(tmp_path: Path) -> None:
    base = tmp_path / "solramp.log"
    handler = TimeBucketFileHandler(base, 3600)
    handler.setFormatter(logging.Formatter("%(message)s"))
    first = handler.current_path
    next_year = datetime.now().replace(year=datetime.now().year + 1)
    try:
        handler.handle(_record(datetime.now(), "now"))
        handler.handle(_record(next_year, "next year"))
    finally:
        handler.close()

    files = rotated_log_files(base)
    assert files[0] == first
    assert files[0].read_text(encoding="utf-8") == "now\n"
    assert files[1].read_text(encoding="utf-8") == "next year\n"


def test_handler_never_moves_back_to_an_older_bucket(tmp_path: Path) -> None:
    base = tmp_path / "solramp.log"
    handler = TimeBucketFileHandler(base, 3600)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.handle(_record(datetime.now(), "now"))
        # DST fall-back / NTP step: the clock jumps back into an earlier bucket.
        handler.handle(_record(datetime(2000, 1, 1), "clock went back"))
    finally:
        handler.close()

    (only,) = rotated_log_files(base)
    assert only.read_text(encoding="utf-8") == "now\nclock went back\n"


def test_handler_continues_after_a_newer_existing_file(tmp_path: Path) -> None:
    base = tmp_path / "solramp.log"
    future = log_bucket_path(base, datetime(2999, 1, 1))
    future.write_text("earlier run\n", encoding="utf-8")

    handler = TimeBucketFileHandler(base, 3600)
    assert handler.current_path == future
    handler.close()


def test_handler_creates_no_file_until_something_is_logged(tmp_path: Path) -> None:
    base = tmp_path / "solramp.log"
    TimeBucketFileHandler(base, 3600).close()
    assert rotated_log_files(base) == []


def test_split_log_files_keeps_only_the_newest_rotated_file_active(
    tmp_path: Path,
) -> None:
    base = tmp_path / "solramp.log"
    legacy = base
    legacy.write_text("x", encoding="utf-8")
    old = log_bucket_path(base, datetime(2026, 10, 16, 9, 0))
    new = log_bucket_path(base, datetime(2026, 10, 16, 10, 0))
    for path in (old, new):
        path.write_text("x", encoding="utf-8")
    (tmp_path / "other_2026-10-16-10-00-00.log").write_text("x", encoding="utf-8")

    assert split_log_files(base, rotating=True) == ([legacy, old], new)
    assert split_log_files(base, rotating=False) == ([old, new], legacy)


def test_split_log_files_handles_an_empty_folder(tmp_path: Path) -> None:
    base = tmp_path / "missing" / "solramp.log"
    assert split_log_files(base, rotating=True) == ([], None)
    assert split_log_files(base, rotating=False) == ([], None)


def test_configure_logging_writes_into_the_current_bucket(tmp_path: Path) -> None:
    base = tmp_path / "session" / "solramp.log"
    configure_logging("WARNING", log_file=base, file_level="DEBUG", rotate_seconds=600)
    logging.getLogger("solramp.test").debug("hello")
    for handler in logging.getLogger().handlers:
        handler.flush()

    (path,) = rotated_log_files(base)
    assert "hello" in path.read_text(encoding="utf-8")
    assert not base.exists()
