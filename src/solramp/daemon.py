# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daemon supervisor: wait for the camera, run one session, repeat.

Each session gets its own timestamped output folder.  A session ends when the
camera has been unusable for ``camera_lost_cycles * interval_seconds`` (see
:meth:`solramp.pipeline.Pipeline._check_camera_lost`); the supervisor then
goes back to waiting and the *next* session starts in a new folder.

This module owns the only signal handlers in the process, so ``run()`` is
always called with an explicit stop event.
"""

from __future__ import annotations

import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .app import EXIT_CAMERA_LOST, EXIT_FATAL, EXIT_NOT_READY, EXIT_OK, run
from .camera_factory import create_camera
from .camera_transport import CameraTransport, MissingDependencyError
from .config import Config, build_config
from .logging_setup import configure_logging, get_logger
from .visualizer import start_visualizer

_log = get_logger("daemon")

# Backoff ladder (seconds) for a camera that is reachable but cannot shoot.
_BACKOFF_SECONDS = (30.0, 60.0, 120.0, 300.0)


def _backoff(step: int) -> float:
    return _BACKOFF_SECONDS[min(step, len(_BACKOFF_SECONDS) - 1)]


def _install_handlers(stop: threading.Event) -> None:
    def _handle(signum: int, _frame: object) -> None:
        _log.info("Received %s; stopping the daemon", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def supervise(cli_overrides: dict[str, Any], toml_path: Path | None) -> int:
    """Run sessions back to back until stopped. Returns a process exit code."""
    base = build_config(cli_overrides, toml_path=toml_path)
    stop = threading.Event()
    _install_handlers(stop)

    # Transports are frozen and stateless, so one is reused for every poll; each
    # session still gets a fresh Camera (PTP-IP handle lifetimes are delicate).
    try:
        transport = create_camera(base).transport
    except (ValueError, MissingDependencyError) as exc:
        _log.error("Cannot start: %s", exc)
        return EXIT_FATAL
    _log.info(
        "Daemon mode: watching %s every %gs; a session ends after %g missed "
        "intervals (%gs)",
        transport.describe(),
        base.daemon_poll_seconds,
        base.camera_lost_cycles,
        base.camera_lost_cycles * base.interval_seconds,
    )

    # One dashboard for the whole daemon: it stays reachable between sessions
    # and the port is bound only once.
    visualizer = start_visualizer(base) if base.visualizer else None
    try:
        return _supervise_loop(base, cli_overrides, toml_path, transport, stop)
    finally:
        if visualizer is not None:
            visualizer.stop()
        _log.info("Daemon stopped")


def _supervise_loop(
    base: Config,
    cli_overrides: dict[str, Any],
    toml_path: Path | None,
    transport: CameraTransport,
    stop: threading.Event,
) -> int:
    backoff_step = 0
    while not stop.is_set():
        if not _wait_for_camera(transport, base, stop):
            break

        # Built only after the camera appears so the folder name is the real
        # session start time, and no empty dated folder is left behind while idle.
        config = build_config(cli_overrides, toml_path=toml_path, now=datetime.now())
        configure_logging(
            config.log_level_stdout,
            log_file=config.log_file_path,
            file_level=config.log_level_file,
            rotate_seconds=config.log_rotate_seconds,
        )
        _log.info("Starting session in %s", config.output_dir)
        started = time.monotonic()
        try:
            code = run(config, stop_event=stop)
        except Exception:  # noqa: BLE001 - one bad session must not end the night
            _log.exception("Session crashed; treating it as a failed session")
            code = EXIT_NOT_READY
        finally:
            # Back to stdout-only: closes the session log file so idle output
            # does not keep writing into a finished session folder.
            configure_logging(base.log_level_stdout)
        elapsed = time.monotonic() - started

        if stop.is_set():
            break
        if code == EXIT_FATAL:
            _log.error("Session failed fatally (exit %d); the daemon is giving up", code)
            return code

        backoff_step = _next_backoff_step(base, code, elapsed, backoff_step)
        delay = _backoff(backoff_step) if backoff_step else base.daemon_poll_seconds
        _log.info(
            "Session ended after %.0fs (exit %d); next attempt in %.0fs",
            elapsed, code, delay,
        )
        if stop.wait(delay):
            break

    return EXIT_OK


def _wait_for_camera(
    transport: CameraTransport, base: Config, stop: threading.Event
) -> bool:
    if transport.is_reachable():
        return True
    _log.info("Waiting for camera %s ...", transport.describe())
    if not transport.wait_for_arrival(stop, poll=base.daemon_poll_seconds):
        return False
    _log.info("Camera %s is reachable", transport.describe())
    return True


def _next_backoff_step(base: Config, code: int, elapsed: float, step: int) -> int:
    """Escalate while sessions keep failing, reset once one actually ran.

    A camera that answers but cannot shoot (no card, lens error) would otherwise
    spin the loop and litter the output directory with empty dated folders.
    """
    productive = elapsed >= 2 * base.interval_seconds
    if code == EXIT_CAMERA_LOST and productive:
        return 0
    if code in (EXIT_NOT_READY, EXIT_CAMERA_LOST):
        return step + 1
    return 0
