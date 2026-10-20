# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional ``--auto-init``: find a safe ETTR starting exposure automatically.

Strategy: begin **maximally underexposed** (lowest ISO, narrowest aperture,
fastest shutter) - which can never damage anything - then add light quickly
until the highlights reach the ETTR target (with the configured headroom
margin).  Coarse jumps are used while far from target and single steps near it;
if a frame clips white we step back once and stop.

All test frames are written to a temporary ``.autoinit`` sub-directory. They are
kept by default (``auto_init_keep_files``) so the initialisation can be
reviewed, but can be deleted automatically when that option is disabled. With a
remote sync, each frame is handed off right after it has been analysed, exactly
like a regular capture.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path

from .camera_tables import ExposureAxis
from .camera import Camera
from .config import Config
from .logging_setup import get_logger
from .ramping import (
    ExposureState,
    LightDirection,
    apply_light_change,
    target_error_ev,
)
from .raw_analysis import analyze_cr2

_log = get_logger("autoinit")

# Roughly one camera table step is ~1/3 stop; used to size coarse jumps.
_STEP_STOPS = 1.0 / 3.0
_MAX_ITERATIONS = 25
_MAX_STEPS_PER_ITER = 6  # at most ~2 stops per jump to stay gentle


def auto_initialize(
    camera: Camera,
    config: Config,
    aperture_axis: ExposureAxis,
    *,
    on_frame: Callable[[Path], None] | None = None,
    on_finish: Callable[[], None] | None = None,
) -> ExposureState:
    """Return a good starting :class:`ExposureState`.

    ``on_frame`` receives each test frame right after its analysis (the sync
    hand-off). ``on_finish`` runs before the test frames are kept or deleted
    according to ``config.auto_init_keep_files``, e.g. to let uploads drain.
    """
    params = config.ramp
    state = ExposureState(
        iso=params.iso_dark_limit,
        aperture=params.aperture_dark_limit,
        shutter=params.shutter_dark_limit,
    )
    target = params.target_headroom_stops
    tmp_dir = config.output_dir / ".autoinit"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    _log.info("Auto-init: starting underexposed at %s and ramping up to ETTR", state)

    try:
        for iteration in range(_MAX_ITERATIONS):
            camera.apply_exposure_state(state)
            frame = camera.capture(state)
            dest = tmp_dir / f"init_{iteration:02d}.{frame.extension}"
            camera.download(frame, dest)
            try:
                metrics = analyze_cr2(
                    dest,
                    half_size=params.half_size_decode,
                    white_clip_frac=params.white_clip_frac,
                    black_clip_frac=params.black_clip_frac,
                    highlight_percentile=params.highlight_percentile,
                )
            finally:
                if on_frame is not None:
                    on_frame(dest)
            error = target_error_ev(metrics, target)  # +ve == still too dark
            white_clipping = metrics.clipping_white_pct > params.white_clip_pct_threshold
            _log.info(
                "Auto-init %02d: %s -> hi_ev=%.2f error=%+.2f white=%.2f%% black=%.2f%%",
                iteration,
                state,
                metrics.highlight_ev,
                error,
                metrics.clipping_white_pct,
                metrics.clipping_black_pct,
            )

            if white_clipping:
                stepped, axis, _ = apply_light_change(
                    state,
                    LightDirection.REMOVE,
                    1,
                    params,
                    aperture_axis=aperture_axis,
                )
                if axis is not None:
                    state = stepped
                _log.info(
                    "Auto-init: backed off white clipping, final %s (took %.1fs)",
                    state, time.monotonic() - started,
                )
                break

            if error <= params.deadband_stops:
                _log.info("Auto-init: reached target, final %s (took %.1fs)",
                          state, time.monotonic() - started)
                break

            steps = max(1, min(_MAX_STEPS_PER_ITER, round(error / _STEP_STOPS)))
            new_state, axis, _ = apply_light_change(
                state,
                LightDirection.ADD,
                steps,
                params,
                aperture_axis=aperture_axis,
            )
            if axis is None:
                _log.warning(
                    "Auto-init: hit exposure limits while still %.2f EV dark; "
                    "using %s",
                    error,
                    state,
                )
                break
            state = new_state
        else:
            _log.warning("Auto-init: stopped after %d iterations at %s",
                         _MAX_ITERATIONS, state)
    finally:
        if on_finish is not None:
            on_finish()
        if config.auto_init_keep_files and config.output_dir_in_memory:
            # Anything still here failed to upload; the pipeline never sweeps
            # .autoinit/, so on tmpfs it would pin ~35 MB a frame all session.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _log.warning(
                "Auto-init: deleted test frames anyway - %s is RAM-backed",
                config.output_dir,
            )
        elif config.auto_init_keep_files and not any(tmp_dir.iterdir()):
            # --delete-synced-raw already freed every shipped frame.
            tmp_dir.rmdir()
            _log.info("Auto-init: test frames were shipped and freed locally")
        elif config.auto_init_keep_files:
            _log.info("Auto-init: kept temporary test frames in %s", tmp_dir)
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _log.info("Auto-init: deleted temporary test frames")

    return state
