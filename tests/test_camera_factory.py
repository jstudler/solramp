# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for camera transport selection in the camera factory."""

from __future__ import annotations

from datetime import datetime

import pytest

from solramp import camera_transport
from solramp.camera_factory import create_camera
from solramp.camera_transport import MissingDependencyError
from solramp.config import build_config

NOW = datetime(2024, 6, 21, 18, 0, 0)


@pytest.fixture(autouse=True)
def _binaries_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dependency checks must not depend on what is installed on the test host."""
    monkeypatch.setattr(camera_transport.shutil, "which", lambda name: f"/usr/bin/{name}")


def _usb_config(**overrides: object):
    cli = {
        "interval_seconds": 30,
        "camera_transport": "usb",
        "camera_selector": "001,002",
    }
    cli.update(overrides)
    return build_config(cli, now=NOW)


def test_factory_creates_ptpip_camera() -> None:
    config = build_config(
        {
            "interval_seconds": 30,
            "camera_transport": "ptpip",
            "camera_address": "1.2.3.4",
        },
        now=NOW,
    )
    camera = create_camera(config)
    assert camera.transport.name == "ptpip"
    assert camera.transport.describe() == "ptpip:1.2.3.4"


def test_factory_creates_usb_camera() -> None:
    camera = create_camera(_usb_config())
    assert camera.transport.name == "usb"
    assert camera.transport.describe() == "usb:001,002"


def test_factory_fails_when_gphoto2_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera_transport.shutil, "which", lambda name: None)
    with pytest.raises(MissingDependencyError, match="gphoto2"):
        create_camera(_usb_config())


def test_factory_fails_when_ping_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera_transport.shutil, "which", lambda name: None)
    config = build_config(
        {
            "interval_seconds": 30,
            "camera_transport": "ptpip",
            "camera_address": "1.2.3.4",
        },
        now=NOW,
    )
    with pytest.raises(MissingDependencyError, match="ping"):
        create_camera(config)
