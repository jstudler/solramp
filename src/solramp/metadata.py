# SPDX-License-Identifier: AGPL-3.0-or-later
"""YAML side-car metadata store with crash-safe writes and resume support.

The store keeps one file (``timelapse_metadata.yaml`` by default) inside the
capture directory.  Every analysed frame appends a :class:`FrameRecord`.  On
restart the same file is loaded so the session can resume; the caller can ask
for the time gap since the last analysed frame and for the set of already
analysed filenames (so it does not re-process them).

Writes are atomic (write a temp file, then ``os.replace``) and guarded by a lock
so the analyser thread never corrupts the file.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .camera_tables import shutter_label_to_seconds
from .exposure import FrameMetrics


@dataclass
class FrameRecord:
    """One analysed frame's metadata, as written to YAML."""

    filename: str
    timestamp_analysis: str  # ISO-8601 local time the analysis completed
    analysis_duration_ms: float  # wall time spent decoding + measuring the raw
    transfer_duration_ms: float | None  # camera->host download; None if not downloaded by us
    iso: str
    aperture: str
    shutter: str
    shutter_seconds: float
    clipping_white_pct: float
    clipping_black_pct: float
    mean_ev: float
    highlight_ev: float
    mean_saturation: float
    error_ev: float
    phase: str
    ramp_applied: str  # human description of any ramp, or "none"

    @classmethod
    def from_analysis(
        cls,
        *,
        filename: str,
        iso: str,
        aperture: str,
        shutter: str,
        metrics: FrameMetrics,
        error_ev: float,
        phase: str,
        ramp_applied: str,
        analysis_duration_ms: float,
        transfer_duration_ms: float | None = None,
        analysed_at: datetime | None = None,
    ) -> "FrameRecord":
        analysed_at = analysed_at or datetime.now()
        return cls(
            filename=filename,
            timestamp_analysis=analysed_at.isoformat(timespec="seconds"),
            analysis_duration_ms=round(analysis_duration_ms, 3),
            transfer_duration_ms=(
                None if transfer_duration_ms is None else round(transfer_duration_ms, 3)
            ),
            iso=iso,
            aperture=aperture,
            shutter=shutter,
            shutter_seconds=round(shutter_label_to_seconds(shutter), 6),
            clipping_white_pct=round(metrics.clipping_white_pct, 4),
            clipping_black_pct=round(metrics.clipping_black_pct, 4),
            mean_ev=round(metrics.mean_ev, 4),
            highlight_ev=round(metrics.highlight_ev, 4),
            mean_saturation=round(metrics.mean_saturation, 4),
            error_ev=round(error_ev, 4),
            phase=phase,
            ramp_applied=ramp_applied,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MetadataStore:
    """Thread-safe, crash-safe YAML metadata side-car."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self._data = data
        self._lock = threading.Lock()
        self._filenames: set[str] = {
            frame["filename"] for frame in data.get("frames", [])
        }

    # -- construction ----------------------------------------------------
    @classmethod
    def load_or_create(
        cls, path: Path, *, session: dict[str, Any]
    ) -> tuple["MetadataStore", bool]:
        """Load an existing store or create a new one.

        Returns ``(store, resumed)`` where ``resumed`` is True if an existing
        metadata file was found.
        """
        if path.is_file():
            with open(path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            data.setdefault("session", {})
            data.setdefault("frames", [])
            data["session"].setdefault("runs", [])
            data["session"]["runs"].append(session)
            store = cls(path, data)
            store.save()
            return store, True

        data = {"session": dict(session, runs=[session]), "frames": []}
        store = cls(path, data)
        store.save()
        return store, False

    # -- queries ---------------------------------------------------------
    @property
    def frame_count(self) -> int:
        return len(self._data["frames"])

    def analysed_filenames(self) -> set[str]:
        with self._lock:
            return set(self._filenames)

    def has_frame(self, filename: str) -> bool:
        with self._lock:
            return filename in self._filenames

    def last_analysis_time(self) -> datetime | None:
        """Local-time datetime of the most recently analysed frame, if any."""
        frames = self._data["frames"]
        if not frames:
            return None
        try:
            return datetime.fromisoformat(frames[-1]["timestamp_analysis"])
        except (KeyError, ValueError):
            return None

    # -- mutation --------------------------------------------------------
    def append(self, record: FrameRecord) -> None:
        with self._lock:
            self._data["frames"].append(record.to_dict())
            self._filenames.add(record.filename)
            self._save_locked()

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            yaml.safe_dump(self._data, handle, sort_keys=False, default_flow_style=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
