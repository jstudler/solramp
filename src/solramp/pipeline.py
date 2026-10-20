# SPDX-License-Identifier: AGPL-3.0-or-later
"""Top-level orchestration: the capture / download / analyse pipeline.

Three cooperating threads share one camera (serialised by ``camera.lock``):

* **capture loop** - drift-corrected timer; takes a frame, hands it to the
  downloader, then applies any pending ramp change inside the safe window
  (after the capture, finishing >= 1 s before the next one), honouring the
  "two identical frames in a row" rule.
* **download loop** - pulls each captured frame off the camera to a timestamped
  file (atomic rename), deletes it from the card, and hands the path + the exact
  settings used straight to the analyser.
* **analyse loop** - decodes the CR2, compensates for pipeline lag, asks the
  ramping engine for a decision, writes the metadata row, and posts any change
  back to the capture loop.

A watchdog observer on the output directory is a *secondary* safety net: it
catches any ``.cr2`` that appears without going through the direct hand-off
(e.g. files already on disk when resuming).  Everything is de-duplicated by
filename so a frame is never analysed twice.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import gphoto2 as gp
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .camera_tables import APERTURE_AXIS
from .camera import Camera, CameraError, CapturedFrame
from .config import Config
from .logging_setup import get_logger
from .metadata import FrameRecord, MetadataStore
from .ramping import ExposureState, RampingEngine, target_error_ev
from .raw_analysis import analyze_cr2
from .sync import RemoteSync

_log = get_logger("pipeline")

# gphoto2 error codes that indicate the PTP port is stuck after a camera reboot.
_IO_ERROR_CODES = frozenset({-110, -53})  # GP_ERROR_IO_IN_PROGRESS, GP_ERROR_IO
_FILE_NOT_FOUND_CODE = -6                  # GP_ERROR_FILE_NOT_FOUND


@dataclass
class AnalysisJob:
    """A downloaded frame waiting to be analysed."""

    path: Path
    capture_time: float
    settings: ExposureState | None  # None for resumed / externally-found files
    live: bool  # True only for freshly captured frames that may drive ramping
    transfer_duration_ms: float | None = None  # None for files we did not download


class _NewCr2Handler(FileSystemEventHandler):
    """Watchdog handler that forwards completed ``.cr2`` files to the pipeline."""

    def __init__(self, pipeline: "Pipeline") -> None:
        self._pipeline = pipeline

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_enqueue(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        # os.replace() of the ".part" temp file shows up as a moved event.
        self._maybe_enqueue(event, use_dest=True)

    def _maybe_enqueue(self, event: FileSystemEvent, *, use_dest: bool = False) -> None:
        if event.is_directory:
            return
        raw_path = getattr(event, "dest_path", None) if use_dest else event.src_path
        if not raw_path:
            return
        path = Path(raw_path)
        if path.suffix.lower() != ".cr2":
            return
        self._pipeline.enqueue_existing_file(path)


class Pipeline:
    """Owns the worker threads and the shared ramping state."""

    def __init__(
        self,
        config: Config,
        camera: Camera,
        engine: RampingEngine,
        metadata: MetadataStore,
        syncer: RemoteSync | None = None,
    ) -> None:
        self.config = config
        self.camera = camera
        self.engine = engine
        self.metadata = metadata
        self.syncer = syncer

        self._stop = threading.Event()
        # Set when no capture has succeeded for camera_lost_cycles * interval.
        self.camera_lost = threading.Event()
        self._lost_after = config.camera_lost_cycles * config.interval_seconds
        self._last_success = time.monotonic()
        self._download_queue: "queue.Queue[CapturedFrame]" = queue.Queue()
        self._analysis_queue: "queue.Queue[AnalysisJob]" = queue.Queue()

        # Shared ramping state (capture loop owns it; analyser proposes changes).
        self._state_lock = threading.Lock()
        self._current_settings: ExposureState | None = None
        self._frames_since_change = 0
        self._pending: tuple[ExposureState, str] | None = None  # (target, description)

        # De-dup of filenames that are queued or already analysed.
        self._known_lock = threading.Lock()
        self._known: set[str] = set(metadata.analysed_filenames())
        self._last_live_capture_time = 0.0
        # Gate for _decide_live: reject only frames older than the last one
        # *analysed*, not older than the last one captured. When the cycle
        # overruns the interval (long exposures + LENR), the analysis thread
        # always lags behind the capture thread; using the capture time here
        # would discard every decision.
        self._last_analysed_capture_time = 0.0

        self._threads: list[threading.Thread] = []
        self._observer: Observer | None = None

    # -- public state access --------------------------------------------
    def current_settings(self) -> ExposureState:
        with self._state_lock:
            assert self._current_settings is not None
            return self._current_settings

    def set_pending(self, target: ExposureState, description: str) -> None:
        with self._state_lock:
            self._pending = (target, description)

    def capture_alive(self) -> bool:
        """True while the capture thread is still running."""
        return any(t.name == "capture" and t.is_alive() for t in self._threads)

    # -- lifecycle ------------------------------------------------------
    def start(self, initial_settings: ExposureState) -> None:
        with self._state_lock:
            self._current_settings = initial_settings
        # Start the loss clock here, not in __init__: auto-init can run for
        # minutes between construction and the first capture.
        self._last_success = time.monotonic()
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self._resume_scan()
        self._start_observer()
        if self.syncer is not None:
            self.syncer.start()
        for name, target in (
            ("capture", self._capture_loop),
            ("download", self._download_loop),
            ("analyse", self._analyse_loop),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        _log.info(
            "Pipeline started: interval=%gs, output=%s, start settings %s",
            self.config.interval_seconds,
            self.config.output_dir,
            initial_settings,
        )

    def stop(self) -> None:
        """Signal a graceful shutdown (idempotent)."""
        if not self._stop.is_set():
            _log.info("Shutdown requested; finishing capture and pending downloads")
        self._stop.set()

    def join(self) -> None:
        """Wait for all work to drain, then stop the observer."""
        capture_thread = next((t for t in self._threads if t.name == "capture"), None)
        if capture_thread:
            capture_thread.join()
        # Let the download + analyse queues drain after capture has stopped.
        self._download_queue.join()
        self._analysis_queue.join()
        for thread in self._threads:
            thread.join(timeout=5.0)
        # Only now is the full set of frames known to the syncer.
        if self.syncer is not None:
            self.syncer.stop()
            self.syncer.join()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

    # -- resume / watchdog ----------------------------------------------
    def _resume_scan(self) -> None:
        """Queue any CR2s already on disk that the metadata has not yet seen."""
        existing = sorted(self.config.output_dir.glob("*.cr2"))
        for path in existing:
            self.enqueue_existing_file(path)

    def _start_observer(self) -> None:
        self._observer = Observer()
        self._observer.schedule(
            _NewCr2Handler(self), str(self.config.output_dir), recursive=False
        )
        self._observer.start()

    def enqueue_existing_file(self, path: Path) -> None:
        """Enqueue a file found on disk (resume / watchdog) for analysis once."""
        name = path.name
        with self._known_lock:
            if name in self._known:
                return
            self._known.add(name)
        self._analysis_queue.put(
            AnalysisJob(
                path=path,
                capture_time=path.stat().st_mtime if path.exists() else time.time(),
                settings=None,
                live=False,
            )
        )

    # -- capture loop ---------------------------------------------------
    def _capture_loop(self) -> None:
        interval = self.config.interval_seconds
        next_capture = time.monotonic()
        while not self._stop.is_set():
            self._sleep_until(next_capture)
            if self._stop.is_set():
                break

            cycle_start = time.monotonic()
            # Apply any pending ramp *before* the shutter fires. PTP config
            # writes are synchronous, so the new exposure is fully committed by
            # the time capture() runs. Doing it here (rather than in a narrow
            # window before the *next* capture) means a long-exposure cycle that
            # overruns the interval can never starve the ramp - the bug that
            # froze the shutter and let the night go black.
            self._apply_pending()

            settings = self.current_settings()
            capture_start = time.monotonic()
            frame = self._capture_with_retries(settings)
            capture_secs = time.monotonic() - capture_start
            scheduled = next_capture
            # If the cycle overran, shoot again immediately and let the
            # cadence drift; never burst to catch up on missed slots.
            next_capture = max(next_capture + interval, time.monotonic())
            if frame is None:
                self._check_camera_lost()
                continue
            self._last_success = time.monotonic()

            with self._state_lock:
                self._frames_since_change += 1
            with self._known_lock:
                # Reserve the eventual filename so the watchdog won't re-add it.
                self._known.add(self._dest_path_for(frame).name)
            self._download_queue.put(frame)
            self._last_live_capture_time = frame.capture_time

            now = time.monotonic()
            _log.debug(
                "Capture cycle: shutter=%s capture=%.1fs cycle=%.1fs "
                "slack=%.1fs (interval=%gs)",
                settings.shutter,
                capture_secs,
                now - cycle_start,
                next_capture - now,
                interval,
            )

            behind = now - (scheduled + interval)
            if behind > 0:
                _log.warning(
                    "Capture cycle is behind schedule by %.1fs (shutter=%s, "
                    "capture call took %.1fs; interval %gs is too short for the "
                    "current exposure + transfer time). The ramp is still being "
                    "applied; the capture cadence will stretch.",
                    behind,
                    settings.shutter,
                    capture_secs,
                    interval,
                )

    def _capture_with_retries(self, settings: ExposureState) -> CapturedFrame | None:
        for attempt in range(1, self.config.capture_retries + 1):
            if self._stop.is_set():
                return None
            try:
                return self.camera.capture(settings)
            except gp.GPhoto2Error as exc:
                _log.warning(
                    "Capture failed (attempt %d/%d): %s",
                    attempt,
                    self.config.capture_retries,
                    exc,
                )
                if exc.code in _IO_ERROR_CODES:
                    self._reconnect_camera()
                    time.sleep(2.0)
        _log.error("Capture failed after %d attempts; skipping this slot",
                   self.config.capture_retries)
        return None

    def _reconnect_camera(self) -> None:
        _log.warning("Reconnecting to camera (PTP session lost)...")
        try:
            self.camera.reconnect()
            _log.info("Camera reconnected successfully")
        except CameraError as exc:
            _log.error("Camera reconnect failed: %s", exc)

    def _check_camera_lost(self) -> None:
        """End the session once the camera has been unusable for too long."""
        outage = time.monotonic() - self._last_success
        if outage < self._lost_after:
            _log.warning(
                "No frame for %.0fs; session ends if this reaches %.0fs "
                "(%g x %gs interval)",
                outage,
                self._lost_after,
                self.config.camera_lost_cycles,
                self.config.interval_seconds,
            )
            return
        _log.error(
            "Camera lost: no successful capture for %.0fs (%g cycles); "
            "ending this session",
            outage,
            self.config.camera_lost_cycles,
        )
        self.camera_lost.set()
        self._stop.set()

    def _apply_pending(self) -> None:
        """Apply any pending ramp change to the camera.

        Called at the top of each capture cycle, *before* the shutter fires, so
        the change can never be starved by a cycle that overruns the interval.
        PTP config writes are synchronous, so the new exposure is committed
        before ``capture()`` runs.
        """
        with self._state_lock:
            if self._pending is None:
                return
            target, description = self._pending
            if target == self._current_settings:
                _log.debug("Pending ramp (%s) already matches current %s; clearing",
                           description, self._current_settings)
                self._pending = None
                return
            # 2-same rule: at least two frames must share the current settings
            # before we change again (covers normal steps and skips alike).
            if self._frames_since_change < 2:
                _log.debug(
                    "Ramp pending (%s) held: only %d frame(s) at %s so far (need 2)",
                    description, self._frames_since_change, self._current_settings,
                )
                return
            current = self._current_settings

        apply_start = time.monotonic()
        try:
            self.camera.apply_exposure_state(target, current=current)
        except gp.GPhoto2Error as exc:
            _log.error("Failed to apply ramp %s: %s (will retry next frame)",
                       description, exc)
            return

        with self._state_lock:
            self._current_settings = target
            self._frames_since_change = 0
            self._pending = None
        _log.info("RAMP applied -> %s | %s (in %.2fs)",
                  target, description, time.monotonic() - apply_start)

    # -- download loop --------------------------------------------------
    def _download_loop(self) -> None:
        while True:
            try:
                frame = self._download_queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set() and self._download_queue.empty():
                    return
                continue
            try:
                dest = self._dest_path_for(frame)
                transfer_ms = self._download_with_retries(frame, dest)
                if transfer_ms is not None:
                    self._analysis_queue.put(
                        AnalysisJob(
                            path=dest,
                            capture_time=frame.capture_time,
                            settings=frame.settings,
                            live=True,
                            transfer_duration_ms=transfer_ms,
                        )
                    )
            finally:
                self._download_queue.task_done()

    def _dest_path_for(self, frame: CapturedFrame) -> Path:
        stamp = datetime.fromtimestamp(frame.capture_time).strftime("%Y-%m-%d_%H-%M-%S")
        return self.config.output_dir / f"{stamp}.{frame.extension}"

    def _download_with_retries(
        self, frame: CapturedFrame, dest: Path
    ) -> float | None:
        """Download ``frame``; return the transfer time in ms, or None on failure."""
        for attempt in range(1, self.config.download_retries + 1):
            # _reconnect_camera() can block for ~15s, so a gone camera would
            # otherwise stall shutdown by ~45s for every queued frame.
            if self._stop.is_set():
                return None
            try:
                started = time.perf_counter()
                self.camera.download(frame, dest)
                return (time.perf_counter() - started) * 1000.0
            except gp.GPhoto2Error as exc:
                if exc.code == _FILE_NOT_FOUND_CODE:
                    # Frame was lost from camera RAM (e.g. camera rebooted).
                    _log.warning(
                        "Frame %s/%s no longer on camera (lost after reboot); skipping",
                        frame.camera_folder,
                        frame.camera_name,
                    )
                    return None
                _log.warning(
                    "Download failed (attempt %d/%d): %s",
                    attempt,
                    self.config.download_retries,
                    exc,
                )
                if exc.code in _IO_ERROR_CODES:
                    self._reconnect_camera()
        _log.error("Download failed after %d attempts; ignoring frame %s/%s",
                   self.config.download_retries, frame.camera_folder, frame.camera_name)
        return None

    # -- analyse loop ---------------------------------------------------
    def _analyse_loop(self) -> None:
        while True:
            try:
                job = self._analysis_queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set() and self._analysis_queue.empty():
                    return
                continue
            try:
                self._analyse_one(job)
            except Exception as exc:  # noqa: BLE001 - keep the pipeline alive
                _log.exception("Analysis failed for %s: %s", job.path, exc)
            finally:
                # Hand off here, not inside _analyse_one: a frame that fails to
                # decode must still be shipped and freed, or it pins RAM forever.
                if self.syncer is not None:
                    self.syncer.enqueue(job.path)
                self._analysis_queue.task_done()

    def _analyse_one(self, job: AnalysisJob) -> None:
        if self.metadata.has_frame(job.path.name):
            return
        ramp = self.engine.params
        started = time.perf_counter()
        metrics = analyze_cr2(
            job.path,
            half_size=ramp.half_size_decode,
            white_clip_frac=ramp.white_clip_frac,
            black_clip_frac=ramp.black_clip_frac,
            highlight_percentile=ramp.highlight_percentile,
        )
        analysis_ms = (time.perf_counter() - started) * 1000.0
        now = datetime.now(tz=self.config.location.tzinfo)

        if job.live and job.settings is not None:
            decision = self._decide_live(job, metrics, now)
            frame_settings = job.settings
            error_ev = decision.error_ev
            phase = decision.phase.value
            ramp_applied = decision.describe() if decision.changed else "none"
        else:
            # Historical / externally-found frame: record metrics but never let a
            # stale frame drive the live ramp.
            from .astral_phase import phase_at

            phase = phase_at(self.config.location, now).value
            error_ev = target_error_ev(metrics, ramp.target_headroom_stops)
            frame_settings = job.settings or ExposureState("unknown", "unknown", "unknown")
            ramp_applied = "none (resumed frame)"

        record = FrameRecord.from_analysis(
            filename=job.path.name,
            iso=frame_settings.iso,
            aperture=frame_settings.aperture,
            shutter=frame_settings.shutter,
            metrics=metrics,
            error_ev=error_ev,
            phase=phase,
            ramp_applied=ramp_applied,
            analysis_duration_ms=analysis_ms,
            transfer_duration_ms=job.transfer_duration_ms,
            analysed_at=now,
        )
        self.metadata.append(record)
        _log.info(
            "Analysed %s in %.0fms: white=%.2f%% black=%.2f%% mean_ev=%.2f hi_ev=%.2f -> %s",
            job.path.name,
            analysis_ms,
            metrics.clipping_white_pct,
            metrics.clipping_black_pct,
            metrics.mean_ev,
            metrics.highlight_ev,
            ramp_applied,
        )

    def _decide_live(self, job: AnalysisJob, metrics, now: datetime):
        """Run the ramping engine, compensating for pipeline (download) lag."""
        current = self.current_settings()
        assert job.settings is not None
        # The frame was shot at job.settings, but the camera may already have
        # ramped to `current`. Predict what `current` would meter by shifting the
        # measured EV by the settings delta. This is the "anticipate the delay".
        aperture_axis = getattr(self.engine, "aperture_axis", APERTURE_AXIS)
        lag_stops = current.total_stops(
            aperture_axis=aperture_axis
        ) - job.settings.total_stops(aperture_axis=aperture_axis)
        adjusted = replace(
            metrics,
            highlight_ev=metrics.highlight_ev + lag_stops,
            mean_ev=metrics.mean_ev + lag_stops,
        )
        decision = self.engine.decide(current, adjusted, now)

        # Only the newest live frame is allowed to drive a ramp change.
        # We compare against the last *analysed* capture time (not the last
        # *captured* time) so that when the cycle overruns the interval the
        # analysis decisions are not silently discarded.
        if decision.changed and job.capture_time >= self._last_analysed_capture_time:
            self.set_pending(decision.new_state, decision.describe())
        self._last_analysed_capture_time = max(
            self._last_analysed_capture_time, job.capture_time
        )
        return decision

    # -- helpers --------------------------------------------------------
    def _sleep_until(self, target_monotonic: float) -> None:
        while not self._stop.is_set():
            remaining = target_monotonic - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, 0.5))
