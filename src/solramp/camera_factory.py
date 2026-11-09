# SPDX-License-Identifier: AGPL-3.0-or-later
"""Camera construction from resolved application config."""

from __future__ import annotations

from .camera import Camera
from .camera_transport import CameraTransport, PtpIpTransport, UsbTransport
from .config import Config


def create_camera(config: Config) -> Camera:
    """Build a camera instance for the configured transport."""
    transport = _create_transport(config)
    transport.check_dependencies()
    return Camera(transport)


def _create_transport(config: Config) -> CameraTransport:
    transport = config.camera_transport.lower()
    if transport == "ptpip":
        if not config.camera_address:
            raise ValueError("camera_address is required when camera_transport=ptpip")
        return PtpIpTransport(config.camera_address)
    if transport == "usb":
        return UsbTransport(config.camera_selector)
    raise ValueError(f"unsupported camera_transport {config.camera_transport!r}")
