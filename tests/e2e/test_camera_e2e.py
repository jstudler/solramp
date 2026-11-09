# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end tests that talk to a real Canon EOS 5D Mark IV over PTP-IP.

These are excluded from the default run (``addopts = -m 'not e2e'``) and will be
*skipped* automatically when the camera is not reachable, so a normal
``uv run pytest`` never needs hardware.  Run them explicitly with::

    uv run pytest -m e2e

Point them at a different camera with ``SOLRAMP_CAMERA_ADDRESS``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from solramp.camera import Camera, CameraError
from solramp.camera_transport import PtpIpTransport
from solramp.camera_tables import ISO_AXIS

pytestmark = pytest.mark.e2e

CAMERA_ADDRESS = os.environ.get("SOLRAMP_CAMERA_ADDRESS", "192.168.111.222")

# Canon CR2 files start with "II" + 0x2a00 then the "CR" signature at byte 8.
_CR2_MAGIC = b"CR"


@pytest.fixture(scope="module")
def camera():
    cam = Camera(PtpIpTransport(CAMERA_ADDRESS))
    try:
        # ping-based wait; tolerates the camera briefly recovering its single
        # PTP/IP session slot between connections.
        cam.wait_until_reachable(timeout=30.0)
    except CameraError:
        pytest.skip(f"camera {CAMERA_ADDRESS} not reachable on the network")
    cam.connect()
    try:
        yield cam
    finally:
        cam.disconnect()


def test_camera_reachable() -> None:
    if not Camera(PtpIpTransport(CAMERA_ADDRESS)).is_reachable():
        pytest.skip("camera not reachable")


def test_camera_is_in_manual_mode(camera: Camera) -> None:
    mode = camera.autoexposure_mode()
    assert isinstance(mode, str) and mode
    if not camera.is_manual_mode():
        pytest.skip(f"camera is in {mode!r} mode, not Manual; set the dial to M")


def test_read_exposure_state_returns_valid_labels(camera: Camera) -> None:
    state = camera.read_exposure_state()
    assert ISO_AXIS.is_valid(state.iso)
    assert state.aperture
    assert state.shutter


def test_capture_and_download(camera: Camera, tmp_path: Path) -> None:
    if not camera.is_manual_mode():
        pytest.skip("camera not in Manual mode")
    settings = camera.read_exposure_state()
    frame = camera.capture(settings)
    dest = tmp_path / "e2e_capture.cr2"
    camera.download(frame, dest)
    assert dest.is_file()
    assert dest.stat().st_size > 1_000_000  # a real CR2 is tens of MB
    with open(dest, "rb") as handle:
        header = handle.read(12)
    assert _CR2_MAGIC in header


def test_change_and_restore_iso(camera: Camera) -> None:
    if not camera.is_manual_mode():
        pytest.skip("camera not in Manual mode")
    original = camera.read_exposure_state()
    # Move ISO one step brighter, verify, then restore the original value.
    brighter = ISO_AXIS.step(original.iso, 1)
    if brighter == original.iso:
        brighter = ISO_AXIS.step(original.iso, -1)
    try:
        camera.set_axis("iso", brighter)
        assert camera.read_exposure_state().iso == brighter
    finally:
        camera.apply_exposure_state(original)
        assert camera.read_exposure_state().iso == original.iso
