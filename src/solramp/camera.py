# SPDX-License-Identifier: AGPL-3.0-or-later
"""gphoto2 camera connection management for the Canon EOS 5D Mark IV.

Everything that talks to the camera lives here. A single PTP session cannot
run two transactions at once, so **every** camera operation is serialised through
one re-entrant lock (:pyattr:`Camera.lock`).  Callers that need to do several
operations atomically (e.g. apply a ramp without a download sneaking in) can hold
``camera.lock`` around the whole sequence.

Canon-specific bits (gphoto2 config leaf names, the "Manual" label, capture
target behavior) are isolated in the constants at the top - see the README section
"Hardcoded for the Canon 5D Mark IV".
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import gphoto2 as gp

from .camera_tables import APERTURE_LABELS
from .camera_transport import CameraTransport, CameraTransportError
from .logging_setup import get_logger
from .ramping import ExposureState

_log = get_logger("camera")

# --- Canon EOS gphoto2 config leaf names (camera specific) -----------------
ISO_CONFIG = "iso"
APERTURE_CONFIG = "aperture"
SHUTTER_CONFIG = "shutterspeed"
AUTOEXPOSUREMODE_CONFIG = "autoexposuremode"
CAPTURETARGET_CONFIG = "capturetarget"
LONG_EXPOSURE_NR_CONFIG = "longexpnr"  # not available on all bodies over PTP/IP

# autoexposuremode value that means full manual control.
MANUAL_MODE_LABEL = "Manual"
# capturetarget value. Counter-intuitively this body fails gp_camera_capture()
# with "[-1] Unspecified error" over PTP/IP when the target is the memory card,
# but works when the frame is buffered in the camera's internal RAM and pulled
# from there. We download then delete every frame anyway, so RAM is ideal.
CAPTURE_TARGET_RAM = "Internal RAM"


class CameraError(Exception):
    """Raised for unrecoverable camera/PTP problems."""


@dataclass(frozen=True)
class CapturedFrame:
    """A frame that has been taken and is waiting on the camera to be fetched."""

    camera_folder: str
    camera_name: str
    capture_time: float  # time.time() right after the shutter completed
    settings: ExposureState

    @property
    def extension(self) -> str:
        """Lower-cased file extension reported by the camera (the ``%C`` part)."""
        _, _, ext = self.camera_name.rpartition(".")
        return ext.lower() if ext else "cr2"


class Camera:
    """A connected (or connectable) Canon EOS 5D Mark IV."""

    def __init__(self, transport: CameraTransport) -> None:
        self.transport = transport
        self.lock = threading.RLock()
        self._camera: gp.Camera | None = None
        self._aperture_labels: list[str] = list(APERTURE_LABELS)
        self._long_exposure_nr: bool = True
        # The PortInfo/abilities handed to libgphoto2 point *into* these lists'
        # memory, so the lists must outlive the camera or libgphoto2 reads freed
        # memory and segfaults. Keep references for the whole connection.
        self._port_info_list: gp.PortInfoList | None = None
        self._abilities_list: gp.CameraAbilitiesList | None = None

    # -- reachability ----------------------------------------------------
    def is_reachable(self, timeout: float = 3.0) -> bool:
        return self.transport.is_reachable(timeout=timeout)

    def wait_until_reachable(self, *, timeout: float = 120.0, poll: float = 2.0) -> None:
        """Block until the transport reports the camera as ready."""
        try:
            self.transport.wait_until_reachable(timeout=timeout, poll=poll)
            _log.info("Camera transport %s is reachable", self.transport.describe())
        except CameraTransportError as exc:
            raise CameraError(str(exc)) from exc

    def connect(
        self, *, retries: int = 5, retry_delay: float = 3.0,
        long_exposure_nr: bool = True,
    ) -> None:
        """Open and keep open a camera session, retrying transient failures."""
        with self.lock:
            self._long_exposure_nr = long_exposure_nr
            if self._camera is not None:
                return
            last_error: Exception | None = None
            for attempt in range(1, retries + 1):
                try:
                    prepared = self.transport.prepare_camera()
                    prepared.camera.init()
                    self._camera = prepared.camera
                    self._port_info_list = prepared.port_info_list
                    self._abilities_list = prepared.abilities_list
                    _log.info("Connected to camera via %s", self.transport.describe())
                    self._configure_capture_target()
                    self._load_aperture_labels()
                    if not long_exposure_nr:
                        self._configure_long_exposure_nr(enable=False)
                    return
                except (gp.GPhoto2Error, CameraTransportError) as exc:
                    last_error = exc
                    _log.warning(
                        "Camera init failed (attempt %d/%d): %s",
                        attempt,
                        retries,
                        exc,
                    )
                    time.sleep(retry_delay)
            raise CameraError(
                f"could not initialise camera via {self.transport.describe()}: {last_error}"
            )

    def _configure_capture_target(self) -> None:
        """Buffer captures in the camera's internal RAM.

        On the 5D Mark IV over PTP/IP, ``gp_camera_capture`` fails with
        ``[-1] Unspecified error`` when ``capturetarget`` is the memory card, but
        succeeds and returns the frame path immediately when it is internal RAM.
        We download and delete each frame, so nothing needs to persist on the
        card.
        """
        try:
            self._set_single(CAPTURETARGET_CONFIG, CAPTURE_TARGET_RAM)
        except gp.GPhoto2Error as exc:
            # Capture may still work on the camera default; warn loudly because
            # the wrong target is the most likely cause of capture failures.
            _log.warning(
                "Could not set capturetarget=%r (%s); capture may fail",
                CAPTURE_TARGET_RAM,
                exc,
            )

    def _load_aperture_labels(self) -> None:
        """Read available aperture choices for the mounted lens.

        Falls back to the default 5D Mark IV table when the camera does not
        expose valid aperture choices over PTP.
        """
        try:
            widget = self._require().get_single_config(APERTURE_CONFIG)
            raw_choices = list(widget.get_choices())
        except gp.GPhoto2Error as exc:
            self._aperture_labels = list(APERTURE_LABELS)
            _log.warning(
                "Could not read aperture choices from camera (%s); using default table",
                exc,
            )
            return

        parsed: list[tuple[float, str]] = []
        for raw in raw_choices:
            label = str(raw).strip()
            if not label:
                continue
            try:
                parsed.append((float(label), label))
            except ValueError:
                _log.debug("Ignoring non-numeric aperture choice %r", label)

        if not parsed:
            self._aperture_labels = list(APERTURE_LABELS)
            _log.warning(
                "Camera returned no usable aperture labels; using default table"
            )
            return

        # Darkest to brightest (highest to lowest f-number) to match axis order.
        parsed.sort(key=lambda item: item[0], reverse=True)
        labels: list[str] = []
        seen: set[str] = set()
        for _, label in parsed:
            if label in seen:
                continue
            seen.add(label)
            labels.append(label)
        self._aperture_labels = labels
        _log.info("Aperture labels for mounted lens: %s", ", ".join(labels))

    def aperture_labels(self) -> list[str]:
        """Return the effective aperture table for this camera session."""
        with self.lock:
            return list(self._aperture_labels)

    def _configure_long_exposure_nr(self, *, enable: bool) -> None:
        """Attempt to set in-camera Long Exposure Noise Reduction.

        LENR doubles the blocking time of every long exposure (the camera takes
        an equal-length dark frame) which makes cycles exceed the interval and
        blocks the ramping pipeline. Disabling LENR keeps cycles near the actual
        shutter speed.

        The widget ``longexpnr`` is not always exposed over PTP/IP (the 5D Mk IV
        typically hides it inside ``customfuncex``). When it is not accessible we
        warn the user to disable it via the camera's menu.
        """
        value = "On" if enable else "Off"
        try:
            self._set_single(LONG_EXPOSURE_NR_CONFIG, value)
            _log.info("Long-exposure noise reduction set to %s via PTP", value)
        except gp.GPhoto2Error:
            _log.warning(
                "Could not set Long Exposure NR to %s via PTP (widget %r may not "
                "be available on this body over PTP/IP). Please set it manually "
                "via the camera menu: Shoot Menu 2 -> Long exp. noise reduction "
                "-> %s",
                value,
                LONG_EXPOSURE_NR_CONFIG,
                "Disable" if not enable else "Enable",
            )

    def disconnect(self) -> None:
        with self.lock:
            if self._camera is None:
                return
            try:
                self._camera.exit()
            except gp.GPhoto2Error as exc:
                _log.warning("Error while disconnecting camera: %s", exc)
            finally:
                self._camera = None
                self._aperture_labels = list(APERTURE_LABELS)
                # Safe to release now that the session is closed.
                self._port_info_list = None
                self._abilities_list = None
                _log.info("Disconnected from camera")

    def reconnect(
        self,
        *,
        reachable_timeout: float = 120.0,
        reachable_poll: float = 3.0,
    ) -> None:
        """Tear down the current session and re-establish a fresh one."""
        long_exposure_nr = self._long_exposure_nr
        self.disconnect()
        self.wait_until_reachable(timeout=reachable_timeout, poll=reachable_poll)
        self.connect(long_exposure_nr=long_exposure_nr)

    def _require(self) -> gp.Camera:
        if self._camera is None:
            raise CameraError("camera is not connected")
        return self._camera

    # -- config get/set --------------------------------------------------
    def _get_single(self, name: str) -> str:
        with self.lock:
            widget = self._require().get_single_config(name)
            return widget.get_value()

    def _set_single(self, name: str, value: str) -> None:
        with self.lock:
            camera = self._require()
            widget = camera.get_single_config(name)
            widget.set_value(value)
            camera.set_single_config(name, widget)

    def autoexposure_mode(self) -> str:
        return self._get_single(AUTOEXPOSUREMODE_CONFIG)

    def is_manual_mode(self) -> bool:
        return self.autoexposure_mode() == MANUAL_MODE_LABEL

    def read_exposure_state(self) -> ExposureState:
        """Read the camera's current ISO / aperture / shutter as an ExposureState."""
        with self.lock:
            return ExposureState(
                iso=self._get_single(ISO_CONFIG),
                aperture=self._get_single(APERTURE_CONFIG),
                shutter=self._get_single(SHUTTER_CONFIG),
            )

    def apply_exposure_state(
        self, target: ExposureState, *, current: ExposureState | None = None
    ) -> None:
        """Push ``target`` to the camera, only writing axes that actually change.

        Writing the smallest possible number of PTP transactions keeps the
        change well inside the 1-second-before-next-capture window.
        """
        with self.lock:
            if current is None:
                current = self.read_exposure_state()
            changes: list[str] = []
            if target.iso != current.iso:
                self._set_single(ISO_CONFIG, target.iso)
                changes.append(f"iso {current.iso}->{target.iso}")
            if target.aperture != current.aperture:
                self._set_single(APERTURE_CONFIG, target.aperture)
                changes.append(f"aperture {current.aperture}->{target.aperture}")
            if target.shutter != current.shutter:
                self._set_single(SHUTTER_CONFIG, target.shutter)
                changes.append(f"shutter {current.shutter}->{target.shutter}")
            if changes:
                _log.debug("Applied exposure changes: %s", ", ".join(changes))
            else:
                _log.debug("apply_exposure_state: no axes changed (already at %s)",
                           target)

    def set_axis(self, axis_name: str, label: str) -> None:
        """Set one exposure axis by logical name (iso/aperture/shutter)."""
        config_name = {
            "iso": ISO_CONFIG,
            "aperture": APERTURE_CONFIG,
            "shutter": SHUTTER_CONFIG,
        }[axis_name]
        self._set_single(config_name, label)

    # -- capture / download ---------------------------------------------
    def capture(self, settings: ExposureState) -> CapturedFrame:
        """Trigger the shutter and return where the frame waits on the camera.

        Blocks for the exposure duration (up to 30 s for night shots).  The
        ``settings`` passed in are recorded with the frame so the analyser knows
        exactly which ISO/aperture/shutter produced it.
        """
        with self.lock:
            camera = self._require()
            started = time.monotonic()
            path = camera.capture(gp.GP_CAPTURE_IMAGE)
            elapsed = time.monotonic() - started
            captured = CapturedFrame(
                camera_folder=path.folder,
                camera_name=path.name,
                capture_time=time.time(),
                settings=settings,
            )
            _log.debug(
                "Captured %s/%s (shutter=%s; capture call blocked %.1fs)",
                captured.camera_folder,
                captured.camera_name,
                settings.shutter,
                elapsed,
            )
            return captured

    def download(self, frame: CapturedFrame, dest_path: Path) -> Path:
        """Fetch ``frame`` to ``dest_path`` (atomically) and delete it from camera."""
        with self.lock:
            camera = self._require()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
            started = time.monotonic()
            camera_file = camera.file_get(
                frame.camera_folder, frame.camera_name, gp.GP_FILE_TYPE_NORMAL
            )
            camera_file.save(str(tmp_path))
            os.replace(tmp_path, dest_path)
            elapsed = time.monotonic() - started
            try:
                camera.file_delete(frame.camera_folder, frame.camera_name)
            except gp.GPhoto2Error as exc:
                _log.warning(
                    "Could not delete %s/%s from camera: %s",
                    frame.camera_folder,
                    frame.camera_name,
                    exc,
                )
            size_mb = dest_path.stat().st_size / 1e6
            rate = size_mb / elapsed if elapsed > 0 else 0.0
            _log.debug(
                "Downloaded %s (%.1f MB in %.1fs, %.1f MB/s)",
                dest_path.name,
                size_mb,
                elapsed,
                rate,
            )
            return dest_path

    # -- context manager -------------------------------------------------
    def __enter__(self) -> "Camera":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disconnect()
