# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the crash-safe YAML metadata store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from solramp.metadata import FrameRecord, MetadataStore

from .conftest import make_metrics

SESSION = {"started_at": "2024-06-21T18:00:00", "interval_seconds": 30}


def _record(filename: str = "frame_0001.cr2", **overrides) -> FrameRecord:
    kwargs = dict(
        filename=filename,
        iso="800",
        aperture="8",
        shutter="1/125",
        metrics=make_metrics(highlight_ev=-1.0, white=0.1, black=2.0),
        error_ev=0.0,
        phase="darkening",
        ramp_applied="none",
        analysis_duration_ms=123.4567,
        transfer_duration_ms=456.7891,
    )
    kwargs.update(overrides)
    return FrameRecord.from_analysis(**kwargs)


def test_create_new_store(tmp_path: Path) -> None:
    path = tmp_path / "meta.yaml"
    store, resumed = MetadataStore.load_or_create(path, session=SESSION)
    assert resumed is False
    assert path.is_file()
    assert store.frame_count == 0
    assert store.analysed_filenames() == set()
    assert store.last_analysis_time() is None


def test_append_persists_and_dedups(tmp_path: Path) -> None:
    path = tmp_path / "meta.yaml"
    store, _ = MetadataStore.load_or_create(path, session=SESSION)
    store.append(_record("a.cr2"))
    store.append(_record("b.cr2"))
    assert store.frame_count == 2
    assert store.has_frame("a.cr2")
    assert store.analysed_filenames() == {"a.cr2", "b.cr2"}

    on_disk = yaml.safe_load(path.read_text())
    assert len(on_disk["frames"]) == 2
    assert on_disk["frames"][0]["filename"] == "a.cr2"


def test_resume_keeps_frames_and_appends_run(tmp_path: Path) -> None:
    path = tmp_path / "meta.yaml"
    store, _ = MetadataStore.load_or_create(path, session=SESSION)
    store.append(_record("a.cr2"))

    reopened, resumed = MetadataStore.load_or_create(path, session=SESSION)
    assert resumed is True
    assert reopened.frame_count == 1
    assert reopened.has_frame("a.cr2")

    on_disk = yaml.safe_load(path.read_text())
    assert len(on_disk["session"]["runs"]) == 2  # original + resume run


def test_last_analysis_time_parses_iso(tmp_path: Path) -> None:
    path = tmp_path / "meta.yaml"
    store, _ = MetadataStore.load_or_create(path, session=SESSION)
    when = datetime(2024, 6, 21, 19, 30, 0)
    store.append(_record("a.cr2", analysed_at=when))
    assert store.last_analysis_time() == when


def test_from_analysis_populates_fields() -> None:
    record = _record("x.cr2")
    data = record.to_dict()
    assert data["filename"] == "x.cr2"
    assert data["iso"] == "800"
    assert data["shutter_seconds"] == pytest.approx(0.008)
    assert data["clipping_white_pct"] == pytest.approx(0.1)
    assert data["phase"] == "darkening"
    assert data["analysis_duration_ms"] == pytest.approx(123.457)
    assert data["transfer_duration_ms"] == pytest.approx(456.789)


def test_atomic_write_leaves_no_tmp_file(tmp_path: Path) -> None:
    path = tmp_path / "meta.yaml"
    store, _ = MetadataStore.load_or_create(path, session=SESSION)
    store.append(_record("a.cr2"))
    assert not (tmp_path / "meta.yaml.tmp").exists()
