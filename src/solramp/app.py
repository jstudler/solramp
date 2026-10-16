# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application orchestration: validate the camera, pick a starting exposure,
run the pipeline, and shut down gracefully on SIGINT/SIGTERM.
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime

from . import __version__
from .astral_phase import describe_day
from .autoinit import auto_initialize
from .camera import Camera, CameraError
from .camera_tables import ExposureAxis, build_aperture_axis
from .camera_factory import create_camera
from .camera_transport import MissingDependencyError
from .config import Config
from .logging_setup import get_logger
from .metadata import MetadataStore
from .pipeline import Pipeline
from .ramping import ExposureState, RampingEngine, normalize_aperture_limits
from .visualizer import start_visualizer
from .sync import RemoteSync, SyncError

_log = get_logger("app")

# Exit codes. The daemon supervisor dispatches on these: CAMERA_LOST retries at
# once, NOT_READY retries with backoff, FATAL gives up.
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_CAMERA_LOST = 75
EXIT_NOT_READY = 76


class StartupError(Exception):
    """Raised when a precondition for starting the timelapse is not met."""


def _validate_camera(camera: Camera, config: Config) -> None:
    """Apply the spec's hard start-up preconditions."""
    camera.wait_until_reachable(timeout=120.0)
    camera.connect(long_exposure_nr=config.long_exposure_nr)
    mode = camera.autoexposure_mode()
    if not camera.is_manual_mode():
        raise StartupError(
            f"camera is in '{mode}' exposure mode; set the mode dial to Manual (M) "
            "and restart"
        )
    _log.info("Camera is in Manual mode")


def _initial_settings(
    camera: Camera,
    config: Config,
    aperture_axis: ExposureAxis,
    syncer: RemoteSync | None = None,
) -> ExposureState:
    if config.auto_init:
        if syncer is None:
            return auto_initialize(camera, config, aperture_axis)
        # Ship test frames as they are analysed, like regular captures; the
        # pipeline later reuses this already-running worker.
        syncer.start()
        return auto_initialize(
            camera,
            config,
            aperture_axis,
            on_frame=syncer.enqueue,
            on_finish=syncer.drain,
        )
    state = camera.read_exposure_state()
    _log.info("Using the camera's current exposure as the start point: %s", state)
    return state


def _resolve_runtime_aperture_axis(camera: Camera, config: Config) -> ExposureAxis:
    """Build runtime aperture axis and normalize configured aperture limits."""
    labels = camera.aperture_labels()
    axis = build_aperture_axis(labels)
    normalized, used_fallback = normalize_aperture_limits(config.ramp, axis)
    if used_fallback:
        _log.warning(
            "Configured aperture limits (%s..%s) are not available on this lens; "
            "falling back to full lens range (%s..%s)",
            config.ramp.aperture_dark_limit,
            config.ramp.aperture_bright_limit,
            axis.labels[0],
            axis.labels[-1],
        )
    config.ramp = normalized
    return axis


def _build_session(config: Config, now: datetime, settings: ExposureState) -> dict:
    return {
        "app_version": __version__,
        "started_at": now.isoformat(timespec="seconds"),
        "interval_seconds": config.interval_seconds,
        "camera_transport": config.camera_transport,
        "camera_address": config.camera_address,
        "camera_selector": config.camera_selector,
        "auto_init": config.auto_init,
        "location": {
            "name": config.location.name,
            "latitude": config.location.latitude,
            "longitude": config.location.longitude,
            "timezone": config.location.timezone,
        },
        "initial_settings": {
            "iso": settings.iso,
            "aperture": settings.aperture,
            "shutter": settings.shutter,
        },
    }


def _log_resume(store: MetadataStore, now: datetime) -> None:
    last = store.last_analysis_time()
    if last is None:
        _log.info("Resuming session folder (%d frames, no analysis timestamps)",
                  store.frame_count)
        return
    gap = now - last
    _log.warning(
        "RESUMING existing session: %d frames already recorded; last analysis was "
        "%s ago (at %s)",
        store.frame_count,
        _format_gap(gap.total_seconds()),
        last.isoformat(timespec="seconds"),
    )


def _format_gap(seconds: float) -> str:
    seconds = int(abs(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def run_visualizer_only(config: Config) -> int:
    """Serve the metadata dashboard until interrupted; never touch the camera."""
    server = start_visualizer(config)
    if server is None:
        return EXIT_FATAL

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        _log.info("Received %s; stopping the visualizer", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        stop_event.wait()
    finally:
        server.stop()
    return EXIT_OK


def run(config: Config, *, stop_event: threading.Event | None = None) -> int:
    """Run the timelapse until interrupted. Returns a process exit code.

    When ``stop_event`` is supplied the caller owns the signal handlers and this
    function must not install its own, or every session would clobber them.
    """
    now = datetime.now(tz=config.location.tzinfo)
    _log.info("solramp %s starting", __version__)
    _log.info("Sun for %s today: %s", config.location.name, describe_day(config.location, now))
    _log.info(
        "Max shutter this session: %gs (interval %gs - margin %gs, capped at 30s)",
        config.max_shutter_seconds,
        config.interval_seconds,
        config.shutter_margin_seconds,
    )

    # Before the camera: never shoot a frame there is nowhere to put.
    try:
        syncer = RemoteSync.from_config(config)
        if syncer is not None:
            syncer.preflight()
    except SyncError as exc:
        _log.error("Cannot start: %s", exc)
        return EXIT_NOT_READY

    try:
        return _run_session(config, now, syncer, stop_event)
    except Exception:
        # Logged here, not only by the caller, so the traceback lands in the
        # session log *before* the final push below ships it off the device.
        _log.exception("Session crashed with an unhandled error")
        raise
    finally:
        if syncer is not None:
            _final_push(syncer)


def _final_push(syncer: RemoteSync) -> None:
    # The closing lines are part of the session log, so flush the handlers
    # before the very last side-car push.
    for handler in logging.getLogger().handlers:
        handler.flush()
    try:
        # Normally already stopped by the pipeline, but auto-init starts the
        # worker too and a failed start-up never reaches the pipeline.
        syncer.stop()
        syncer.join()
        syncer.push_sidecars()
    except Exception as exc:  # noqa: BLE001 - must not mask the session result
        _log.warning("Final side-car push failed: %s", exc)
    finally:
        syncer.close()


def _run_session(
    config: Config,
    now: datetime,
    syncer: RemoteSync | None,
    stop_event: threading.Event | None,
) -> int:
    try:
        camera = create_camera(config)
    except (ValueError, MissingDependencyError) as exc:
        _log.error("Cannot start: %s", exc)
        return EXIT_FATAL

    try:
        _validate_camera(camera, config)
    except CameraError as exc:
        _log.error("Cannot start: %s", exc)
        camera.disconnect()
        return EXIT_CAMERA_LOST
    except StartupError as exc:
        _log.error("Cannot start: %s", exc)
        camera.disconnect()
        return EXIT_NOT_READY

    try:
        aperture_axis = _resolve_runtime_aperture_axis(camera, config)
        initial = _initial_settings(camera, config, aperture_axis, syncer)
    except CameraError as exc:
        _log.error("Auto-init failed: %s", exc)
        camera.disconnect()
        return EXIT_CAMERA_LOST
    except ValueError as exc:
        _log.error("Cannot start: %s", exc)
        camera.disconnect()
        return EXIT_FATAL

    store, resumed = MetadataStore.load_or_create(
        config.metadata_path, session=_build_session(config, now, initial)
    )
    if resumed:
        _log_resume(store, now)

    engine = RampingEngine(config.ramp, config.location, aperture_axis=aperture_axis)
    pipeline = Pipeline(config, camera, engine, store, syncer)

    own_signals = stop_event is None
    stop_event = stop_event or threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        _log.info("Received %s; shutting down gracefully", signal.Signals(signum).name)
        pipeline.stop()
        stop_event.set()

    if own_signals:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    # The daemon supervisor keeps one dashboard alive across all its sessions,
    # so only a stand-alone run starts its own.
    visualizer = start_visualizer(config) if own_signals and config.visualizer else None

    exit_code = EXIT_OK
    pipeline.start(initial)
    try:
        # Wait for a shutdown signal, but also exit if the capture thread dies.
        while not stop_event.wait(1.0):
            if syncer is not None and syncer.fatal_error is not None:
                exit_code = EXIT_NOT_READY
                break
            if pipeline.camera_lost.is_set():
                exit_code = EXIT_CAMERA_LOST
                break
            if not pipeline.capture_alive():
                _log.error("Capture thread stopped unexpectedly; shutting down")
                exit_code = EXIT_CAMERA_LOST
                break
    finally:
        pipeline.stop()
        pipeline.join()
        camera.disconnect()
        if visualizer is not None:
            visualizer.stop()
    _log.info("Timelapse stopped. %d frames recorded in %s",
              store.frame_count, config.output_dir)
    return exit_code
