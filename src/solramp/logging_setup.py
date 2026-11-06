# SPDX-License-Identifier: AGPL-3.0-or-later
"""Human-readable stdout logging plus an optional per-session log file.

All errors and all ramping changes go to stdout in a readable, timestamped
format.  Worker threads are named so the log makes the capture/download/analyse
pipeline easy to follow.

A second handler can mirror everything to a file living next to the captures
(the same folder as the CR2s and the YAML sidecar), typically at a more verbose
level (DEBUG) for post-mortem debugging.

With rotation enabled the file handler writes straight into one file per
wall-clock bucket (``solramp_2026-10-16-10-00-00.log`` for 10:00-11:00 with a
one hour interval). Files are never renamed and only ever appended to, which is
what lets the remote sync ship them with ``rsync --append`` and delete the
closed ones locally.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Same format as the session folders, so names sort chronologically.
_BUCKET_FORMAT = "%Y-%m-%d-%H-%M-%S"
_BUCKET_PATTERN = r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}"
# Naive local midnight: every interval that divides a day is aligned to it.
_BUCKET_ANCHOR = datetime(1970, 1, 1)


def log_bucket_path(base: Path, start: datetime) -> Path:
    """Path of the rotated log file for the bucket starting at ``start``."""
    return base.with_name(f"{base.stem}_{start.strftime(_BUCKET_FORMAT)}{base.suffix}")


def bucket_start(moment: datetime, interval_seconds: int) -> datetime:
    """Start of the wall-clock bucket containing the naive local ``moment``."""
    elapsed = int((moment - _BUCKET_ANCHOR).total_seconds())
    return _BUCKET_ANCHOR + timedelta(seconds=elapsed - elapsed % interval_seconds)


def _bucket_regex(base: Path) -> re.Pattern[str]:
    return re.compile(
        rf"^{re.escape(base.stem)}_({_BUCKET_PATTERN}){re.escape(base.suffix)}$"
    )


def rotated_log_files(base: Path) -> list[Path]:
    """All rotated log files for ``base``, oldest first."""
    if not base.parent.is_dir():
        return []
    pattern = _bucket_regex(base)
    return sorted(
        path
        for path in base.parent.iterdir()
        if pattern.match(path.name) and path.is_file()
    )


def split_log_files(base: Path, *, rotating: bool) -> tuple[list[Path], Path | None]:
    """Return ``(closed, active)`` log files for ``base``.

    The active file is the one the handler may still append to: the newest
    rotated file when rotating (names only ever move forward), else ``base``
    itself. Everything else is finished and safe to delete once shipped.
    """
    rotated = rotated_log_files(base)
    legacy = [base] if base.is_file() else []
    if rotating:
        if not rotated:
            return legacy, None
        return legacy + rotated[:-1], rotated[-1]
    return rotated, legacy[0] if legacy else None


class TimeBucketFileHandler(logging.FileHandler):
    """Append to one file per wall-clock bucket of ``interval_seconds``.

    Buckets only ever move forward, so a clock stepping back (DST fall-back,
    NTP correction) keeps writing into the current file instead of re-opening
    an older one that may already have been shipped and deleted.
    """

    def __init__(self, base: Path, interval_seconds: int) -> None:
        self._base = base
        self._interval = int(interval_seconds)
        self._start = bucket_start(datetime.now(), self._interval)
        newest = rotated_log_files(base)
        if newest:
            # Never fall behind a file left by an earlier run in this folder.
            match = _bucket_regex(base).match(newest[-1].name)
            assert match is not None
            self._start = max(
                self._start, datetime.strptime(match.group(1), _BUCKET_FORMAT)
            )
        # delay=True: no empty file appears until something is actually logged.
        super().__init__(
            log_bucket_path(base, self._start), encoding="utf-8", delay=True
        )

    @property
    def current_path(self) -> Path:
        return Path(self.baseFilename)

    def emit(self, record: logging.LogRecord) -> None:
        start = bucket_start(datetime.fromtimestamp(record.created), self._interval)
        if start > self._start:
            # Handler.handle() holds self.lock here, so no other thread writes.
            self._start = start
            if self.stream is not None:
                self.stream.close()
                self.stream = None  # type: ignore[assignment]
            self.baseFilename = os.path.abspath(log_bucket_path(self._base, start))
        super().emit(record)


def _level_value(level: str) -> int:
    return getattr(logging, level.upper(), logging.INFO)


def _make_handler(handler: logging.Handler, level: str) -> logging.Handler:
    """Attach the shared formatter and a level to ``handler``."""
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.setLevel(_level_value(level))
    return handler


def configure_logging(
    stdout_level: str = "INFO",
    *,
    log_file: Path | None = None,
    file_level: str = "DEBUG",
    rotate_seconds: int | None = None,
) -> None:
    """Configure root logging to emit to stdout and, optionally, a file.

    ``stdout_level`` controls the console verbosity.  When ``log_file`` is given,
    a second handler mirrors records to that path at ``file_level`` (its parent
    directory is created if needed).  With ``rotate_seconds`` the file is split
    into wall-clock buckets named after ``log_file`` (see
    :class:`TimeBucketFileHandler`).  The root level is the more verbose of the
    two so each handler can filter independently.
    """
    handlers = [_make_handler(logging.StreamHandler(stream=sys.stdout), stdout_level)]
    levels = [_level_value(stdout_level)]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler: logging.FileHandler = (
            TimeBucketFileHandler(log_file, rotate_seconds)
            if rotate_seconds
            else logging.FileHandler(log_file, encoding="utf-8")
        )
        handlers.append(_make_handler(file_handler, file_level))
        levels.append(_level_value(file_level))

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(min(levels))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"solramp.{name}")
