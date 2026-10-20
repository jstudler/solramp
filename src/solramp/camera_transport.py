# SPDX-License-Identifier: AGPL-3.0-or-later
"""Camera transport backends used by :mod:`solramp.camera`.

The rest of the app should not care whether the camera is reached over PTP-IP
or USB. Each transport implements reachability checks and constructs a
``gp.Camera`` bound to the selected port.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import ClassVar

import gphoto2 as gp


PTPIP_MODEL = "PTP/IP Camera"


class CameraTransportError(Exception):
    """Raised when camera transport discovery/selection fails."""


class MissingDependencyError(CameraTransportError):
    """Raised when an external binary the transport shells out to is missing."""


@dataclass(frozen=True)
class PreparedCamera:
    """Camera object plus list references that must outlive it."""

    camera: gp.Camera
    port_info_list: gp.PortInfoList | None = None
    abilities_list: gp.CameraAbilitiesList | None = None


class CameraTransport:
    """Transport contract consumed by :class:`solramp.camera.Camera`."""

    name: str = "unknown"
    # External binaries this transport shells out to; checked before any use.
    required_binaries: tuple[str, ...] = ()

    def describe(self) -> str:
        raise NotImplementedError

    def check_dependencies(self) -> None:
        """Fail fast when a required host binary is not on PATH."""
        missing = [name for name in self.required_binaries if shutil.which(name) is None]
        if missing:
            raise MissingDependencyError(
                f"required binaries not found on PATH for the {self.name} transport: "
                f"{', '.join(missing)}. Install them (e.g. 'brew install gphoto2' or "
                "'apt install gphoto2') and retry"
            )

    def is_reachable(self, timeout: float = 3.0) -> bool:
        raise NotImplementedError

    def wait_until_reachable(self, *, timeout: float = 120.0, poll: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self.is_reachable():
                return
            if time.monotonic() >= deadline:
                raise CameraTransportError(
                    f"camera transport {self.describe()} not reachable after {timeout:g}s"
                )
            time.sleep(poll)

    def wait_for_arrival(self, stop: threading.Event, *, poll: float = 15.0) -> bool:
        """Block until the camera is reachable. False if ``stop`` was set first.

        Unlike :meth:`wait_until_reachable` this never gives up, and it is the
        seam an event-driven (hotplug/mDNS) transport would override.
        """
        while not self.is_reachable():
            if stop.wait(poll):
                return False
        return True

    def prepare_camera(self) -> PreparedCamera:
        raise NotImplementedError


@dataclass(frozen=True)
class PtpIpTransport(CameraTransport):
    """PTP-IP transport that targets one network address/hostname."""

    address: str
    name: str = "ptpip"
    required_binaries: ClassVar[tuple[str, ...]] = ("ping",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", self.address.split("ptpip:", 1)[-1])

    def describe(self) -> str:
        return f"ptpip:{self.address}"

    def is_reachable(self, timeout: float = 3.0) -> bool:
        try:
            result = subprocess.run(
                ["ping", "-c", "1", self.address],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0

    def prepare_camera(self) -> PreparedCamera:
        port_path = self.describe()
        port_info_list = gp.PortInfoList()
        port_info_list.load()
        try:
            port_index = port_info_list.lookup_path(port_path)
        except gp.GPhoto2Error as exc:
            raise CameraTransportError(
                f"gphoto2 has no PTP-IP port for {port_path!r}; is libgphoto2 "
                "built with PTP/IP support?"
            ) from exc

        abilities_list = gp.CameraAbilitiesList()
        abilities_list.load()
        try:
            model_index = abilities_list.lookup_model(PTPIP_MODEL)
        except gp.GPhoto2Error as exc:
            raise CameraTransportError(
                f"gphoto2 has no {PTPIP_MODEL!r} driver; libgphoto2 PTP/IP "
                "support is missing"
            ) from exc

        camera = gp.Camera()
        camera.set_port_info(port_info_list[port_index])
        camera.set_abilities(abilities_list[model_index])
        return PreparedCamera(
            camera=camera,
            port_info_list=port_info_list,
            abilities_list=abilities_list,
        )


@dataclass(frozen=True)
class UsbTransport(CameraTransport):
    """USB transport selected by optional model/port selector."""

    selector: str | None = None
    name: str = "usb"
    required_binaries: ClassVar[tuple[str, ...]] = ("gphoto2",)

    def describe(self) -> str:
        return f"usb:{self.selector}" if self.selector else "usb:auto"

    def _autodetected_ports(self) -> list[tuple[str, str]]:
        try:
            result = subprocess.run(
                ["gphoto2", "--auto-detect"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            raise CameraTransportError(
                "failed to run 'gphoto2 --auto-detect' for USB camera discovery"
            ) from exc

        entries: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            line = line.rstrip()
            if not line or line.startswith("Model") or line.startswith("-"):
                continue
            if "usb:" not in line:
                continue
            model, _, port = line.rpartition("  ")
            model = model.strip()
            port = port.strip()
            if model and port.startswith("usb:"):
                entries.append((model, port))
        return entries

    def _select_port(self) -> str:
        entries = self._autodetected_ports()
        if not entries:
            raise CameraTransportError("no USB cameras detected")

        if self.selector:
            matches = [
                (model, port)
                for model, port in entries
                if self.selector in model or self.selector in port
            ]
            if not matches:
                raise CameraTransportError(
                    f"no USB camera matched selector {self.selector!r}"
                )
            if len(matches) > 1:
                options = ", ".join(f"{model} [{port}]" for model, port in matches)
                raise CameraTransportError(
                    f"USB selector {self.selector!r} is ambiguous; matches: {options}"
                )
            return matches[0][1]

        if len(entries) > 1:
            options = ", ".join(f"{model} [{port}]" for model, port in entries)
            raise CameraTransportError(
                "multiple USB cameras detected; set camera_selector to choose one: "
                f"{options}"
            )

        return entries[0][1]

    def is_reachable(self, timeout: float = 3.0) -> bool:
        _ = timeout
        try:
            self._select_port()
        except CameraTransportError:
            return False
        return True

    def prepare_camera(self) -> PreparedCamera:
        port_path = self._select_port()
        port_info_list = gp.PortInfoList()
        port_info_list.load()
        try:
            port_index = port_info_list.lookup_path(port_path)
        except gp.GPhoto2Error as exc:
            raise CameraTransportError(
                f"gphoto2 has no USB port for {port_path!r}"
            ) from exc

        camera = gp.Camera()
        camera.set_port_info(port_info_list[port_index])
        return PreparedCamera(camera=camera, port_info_list=port_info_list)
