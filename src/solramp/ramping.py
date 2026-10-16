# SPDX-License-Identifier: AGPL-3.0-or-later
"""The exposure ramping decision engine.

Given the metrics of the most recent frame and the current camera settings, the
engine decides whether (and how) to nudge ISO / aperture / shutter to keep the
exposure on an ETTR target while ramping smoothly from day to night and back.

Design summary (see README for the full rationale):

* We work in *brightness stops* (see :mod:`solramp.camera_tables`).  "Add
  light" makes the image brighter; "remove light" makes it darker.
* The priority of which axis to move is intrinsic to the direction of change and
  matches the spec's priorities (keep ISO low, then shutter fast, then aperture
  narrow):

  - **add light** (scene darkening): aperture -> shutter -> iso
  - **remove light** (scene brightening): iso -> shutter -> aperture

* The astral :class:`~solramp.astral_phase.Phase` gates the *direction* so the
  image only darkens in the evening and only brightens in the morning, except
  for clipping-protection overrides (white clipping is treated as worse than
  black clipping).
* Avoiding clipping (ETTR) is the first priority; tracking the smooth target is
  second.
* A persistent >1 EV trend in one direction triggers a 2-step "skip" so a fast
  dusk/dawn does not outrun a 1/3-stop-per-frame ramp.

The engine only *decides*; enforcement of the "at least two identical frames in
a row" (LRTimelapse) rule and the change-timing window lives in the scheduler.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from enum import IntEnum

from .astral_phase import LocationConfig, Phase, phase_at, sun_altitude
from .camera_tables import (
    APERTURE_AXIS,
    ISO_AXIS,
    SHUTTER_AXIS,
    ExposureAxis,
    build_aperture_axis,
    fastest_shutter_within,
)
from .exposure import FrameMetrics


class LightDirection(IntEnum):
    """Direction of an exposure change, as a brightness-stop sign."""

    ADD = 1  # make the image brighter
    REMOVE = -1  # make the image darker

    @property
    def sign(self) -> int:
        return int(self.value)


# Axis priority by light direction (see module docstring).
_ADD_ORDER = ("aperture", "shutter", "iso")
_REMOVE_ORDER = ("iso", "shutter", "aperture")


def _lerp(day: float, night: float, night_fraction: float) -> float:
    """Linear blend: ``day`` at ``night_fraction`` 0, ``night`` at 1."""
    return day + (night - day) * night_fraction


@dataclass(frozen=True)
class ExposureState:
    """The three exposure values currently set on the camera (exact labels)."""

    iso: str
    aperture: str
    shutter: str

    def total_stops(self, *, aperture_axis: ExposureAxis = APERTURE_AXIS) -> float:
        """Summed brightness contribution of all three axes."""
        return (
            ISO_AXIS.stops_of(self.iso)
            + aperture_axis.stops_of(self.aperture)
            + SHUTTER_AXIS.stops_of(self.shutter)
        )

    def with_value(self, axis_name: str, label: str) -> "ExposureState":
        return replace(self, **{axis_name: label})

    def __str__(self) -> str:
        return f"iso={self.iso} f/{self.aperture} {self.shutter}s"


@dataclass
class RampParams:
    """Tunable thresholds + the per-axis ramp limits.

    Limits are expressed as exact camera labels.  ``*_dark_limit`` is the
    darkest value the engine may select; ``*_bright_limit`` the brightest.  The
    shutter bright (slow) limit is recomputed from ``max_shutter_seconds`` so it
    never exceeds ``interval - margin``.
    """

    target_headroom_stops: float = 1.0  # keep highlights ~1 stop below clipping (DAY)
    deadband_stops: float = 0.33  # ignore exposure errors smaller than this
    white_clip_pct_threshold: float = 0.5  # % white-clipped pixels that forces darkening (DAY)
    black_clip_pct_threshold: float = 20.0  # % black-clipped pixels that lets us brighten past the dead-band
    # --- day/night tolerances (blended smoothly by sun altitude) -----------
    # At night we deliberately expose brighter (lower headroom) and tolerate
    # more blown highlights, so the shadows are not crushed into noise that has
    # to be lifted in post. The strict values above are the *day* values.
    target_headroom_stops_night: float = 0.0  # ETTR right up to clipping at night
    white_clip_pct_threshold_night: float = 5.0  # tolerate blown light sources at night
    # Hard ceiling on crushed-shadow pixels, enforced day AND night. When more
    # than this fraction of the frame is black-clipped the engine adds light to
    # rescue the shadows, overriding the phase gate, the dead-band AND the
    # white-clip protection (keeping shadows beats a clean highlight).
    black_clip_pct_limit: float = 70.0
    # Sun-altitude band (degrees) over which day tolerances blend into night
    # tolerances. >= day_altitude_deg -> full day; <= night_altitude_deg -> full
    # night; linear in between so the exposure target never jumps.
    day_altitude_deg: float = 6.0
    night_altitude_deg: float = -6.0
    # --- proactive dawn ramping -------------------------------------------
    # During the BRIGHTENING phase while the sun is in the twilight band
    # (between night_altitude_deg and day_altitude_deg), if clipping_black_pct
    # falls below this threshold the engine proactively removes light to
    # pre-empt white clipping before it actually fires. This catches the
    # "sky is brightening fast but highlights haven't clipped yet" gap.
    # The issue becomes more dramatic with longer intervals (e.g. 30s) 
    # because the sunrise curve can easily outrun the ramp
    # The issue becomes more dramatic with a delayed analysis which is 
    # often the case with cameras in "tethering" mode via Wi-Fi, where 
    # the frame that triggered the ramp decision may have been captured 
    # several seconds/minutes ago.
    dawn_proactive_black_pct: float = 10.0
    dawn_proactive_mean_ev_window: int = 5  # frames of mean_ev history for rising detection
    highlight_percentile: float = 99.5
    white_clip_frac: float = 0.99
    black_clip_frac: float = 0.01
    half_size_decode: bool = True
    trend_window: int = 3  # consecutive frames to confirm a fast trend
    trend_ev_threshold: float = 1.0  # per-frame EV error magnitude that counts as "fast"

    iso_dark_limit: str = "100"
    iso_bright_limit: str = "32000"
    aperture_dark_limit: str = "22"  # narrowest f allowed
    aperture_bright_limit: str = "2.8"  # widest f allowed
    shutter_dark_limit: str = "1/8000"  # fastest shutter allowed
    max_shutter_seconds: float = 30.0  # slow (bright) shutter cap = interval - margin

    def __post_init__(self) -> None:
        if self.night_altitude_deg >= self.day_altitude_deg:
            raise ValueError(
                f"night_altitude_deg ({self.night_altitude_deg}) must be below "
                f"day_altitude_deg ({self.day_altitude_deg})"
            )
        # Validate fixed-axis limits and ordering.
        for axis, dark, bright in (
            (ISO_AXIS, self.iso_dark_limit, self.iso_bright_limit),
            (SHUTTER_AXIS, self.shutter_dark_limit, self.shutter_bright_limit()),
        ):
            if axis.stops_of(dark) > axis.stops_of(bright):
                raise ValueError(
                    f"{axis.name} dark limit {dark!r} is brighter than bright "
                    f"limit {bright!r}"
                )

    def validate_aperture_limits(self, aperture_axis: ExposureAxis) -> None:
        """Ensure aperture limits exist on the provided runtime lens axis."""
        for label, role in (
            (self.aperture_dark_limit, "dark"),
            (self.aperture_bright_limit, "bright"),
        ):
            if not aperture_axis.is_valid(label):
                raise ValueError(
                    f"aperture {role} limit {label!r} is not available on this lens"
                )
        if aperture_axis.stops_of(self.aperture_dark_limit) > aperture_axis.stops_of(
            self.aperture_bright_limit
        ):
            raise ValueError(
                f"aperture dark limit {self.aperture_dark_limit!r} is brighter than "
                f"bright limit {self.aperture_bright_limit!r}"
            )

    def shutter_bright_limit(self) -> str:
        """Brightest (slowest) shutter allowed given the interval-derived cap."""
        return fastest_shutter_within(self.max_shutter_seconds)

    def night_fraction(self, sun_altitude_deg: float) -> float:
        """0 in full daylight, 1 at/after night, linear across the twilight band."""
        hi, lo = self.day_altitude_deg, self.night_altitude_deg
        if sun_altitude_deg >= hi:
            return 0.0
        if sun_altitude_deg <= lo:
            return 1.0
        return (hi - sun_altitude_deg) / (hi - lo)

    def effective_headroom_stops(self, night_fraction: float) -> float:
        """Highlight headroom for this night fraction (day->night blend)."""
        return _lerp(
            self.target_headroom_stops, self.target_headroom_stops_night, night_fraction
        )

    def effective_white_clip_pct_threshold(self, night_fraction: float) -> float:
        """White-clip tolerance for this night fraction (day->night blend)."""
        return _lerp(
            self.white_clip_pct_threshold,
            self.white_clip_pct_threshold_night,
            night_fraction,
        )

    def axis_limits(
        self, name: str, *, aperture_axis: ExposureAxis = APERTURE_AXIS
    ) -> tuple[ExposureAxis, str, str]:
        if name == "iso":
            return ISO_AXIS, self.iso_dark_limit, self.iso_bright_limit
        if name == "aperture":
            return aperture_axis, self.aperture_dark_limit, self.aperture_bright_limit
        if name == "shutter":
            return SHUTTER_AXIS, self.shutter_dark_limit, self.shutter_bright_limit()
        raise KeyError(name)


def normalize_aperture_limits(
    params: RampParams, aperture_axis: ExposureAxis
) -> tuple[RampParams, bool]:
    """Return params adjusted for this lens, plus whether a fallback was used.

    If either configured aperture limit is unavailable on the mounted lens, both
    limits fall back to the full available range for that lens.
    """
    has_dark = aperture_axis.is_valid(params.aperture_dark_limit)
    has_bright = aperture_axis.is_valid(params.aperture_bright_limit)
    if has_dark and has_bright:
        params.validate_aperture_limits(aperture_axis)
        return params, False
    adjusted = replace(
        params,
        aperture_dark_limit=aperture_axis.labels[0],
        aperture_bright_limit=aperture_axis.labels[-1],
    )
    return adjusted, True


@dataclass(frozen=True)
class RampDecision:
    """Outcome of a single ramping decision."""

    changed: bool
    old_state: ExposureState
    new_state: ExposureState
    error_ev: float
    phase: Phase
    reason: str
    param: str | None = None
    direction: LightDirection | None = None
    steps: int = 0
    applied_stops: float = 0.0

    @classmethod
    def no_change(
        cls,
        state: ExposureState,
        error_ev: float,
        phase: Phase,
        reason: str,
    ) -> "RampDecision":
        return cls(
            changed=False,
            old_state=state,
            new_state=state,
            error_ev=error_ev,
            phase=phase,
            reason=reason,
        )

    def describe(self) -> str:
        if not self.changed:
            return f"hold ({self.reason}; error {self.error_ev:+.2f} EV, {self.phase.value})"
        old = getattr(self.old_state, self.param)
        new = getattr(self.new_state, self.param)
        skip = " [skip]" if self.steps > 1 else ""
        return (
            f"{self.param} {old} -> {new} "
            f"({self.applied_stops:+.2f} stop, {self.direction.name.lower()}{skip}; "
            f"{self.reason}; error {self.error_ev:+.2f} EV, {self.phase.value})"
        )


def apply_light_change(
    state: ExposureState,
    direction: LightDirection,
    steps: int,
    params: RampParams,
    *,
    aperture_axis: ExposureAxis = APERTURE_AXIS,
) -> tuple[ExposureState, str | None, float]:
    """Move the highest-priority axis that can still change in ``direction``.

    Returns ``(new_state, axis_name, applied_stops)``.  ``axis_name`` is ``None``
    (and the state unchanged) when every axis in the priority order has hit its
    limit.
    """
    order = _ADD_ORDER if direction is LightDirection.ADD else _REMOVE_ORDER
    for name in order:
        axis, dark, bright = params.axis_limits(name, aperture_axis=aperture_axis)
        current = getattr(state, name)
        if axis.can_step(current, steps * direction.sign, min_label=dark, max_label=bright):
            new_label = axis.step(
                current, steps * direction.sign, min_label=dark, max_label=bright
            )
            applied = axis.stops_between(current, new_label)
            return state.with_value(name, new_label), name, applied
    return state, None, 0.0


# White-clip severity thresholds for aggressive multi-step remove.
_WHITE_CLIP_SEVERE_PCT = 5.0   # > 5%: 2 steps
_WHITE_CLIP_CRITICAL_PCT = 20.0  # > 20%: 3 steps, shutter-priority


def apply_white_clip_remove(
    state: ExposureState,
    white_clip_pct: float,
    params: RampParams,
    *,
    aperture_axis: ExposureAxis = APERTURE_AXIS,
) -> tuple[ExposureState, str | None, float]:
    """Emergency remove-light for white clipping, scaling with severity.

    Mild clipping (above threshold, <=5%): 1 step, normal priority (iso first).
    Moderate (>5%): 2 steps, normal priority.
    Severe (>20%): 3 steps, shutter-priority (shutter->iso->aperture) so the
    dominant lever at dawn (a 25s exposure) is attacked first.
    """
    if white_clip_pct > _WHITE_CLIP_CRITICAL_PCT:
        steps = 3
        # Shutter-priority: attack the longest exposure first at dawn.
        order = ("shutter", "iso", "aperture")
    elif white_clip_pct > _WHITE_CLIP_SEVERE_PCT:
        steps = 2
        order = _REMOVE_ORDER  # iso -> shutter -> aperture
    else:
        steps = 1
        order = _REMOVE_ORDER

    direction = LightDirection.REMOVE
    for name in order:
        axis, dark, bright = params.axis_limits(name, aperture_axis=aperture_axis)
        current = getattr(state, name)
        if axis.can_step(current, steps * direction.sign, min_label=dark, max_label=bright):
            new_label = axis.step(
                current, steps * direction.sign, min_label=dark, max_label=bright
            )
            applied = axis.stops_between(current, new_label)
            return state.with_value(name, new_label), name, applied
    # Fall back to single step on any axis (we're near limits).
    for name in order:
        axis, dark, bright = params.axis_limits(name, aperture_axis=aperture_axis)
        current = getattr(state, name)
        if axis.can_step(current, 1 * direction.sign, min_label=dark, max_label=bright):
            new_label = axis.step(
                current, 1 * direction.sign, min_label=dark, max_label=bright
            )
            applied = axis.stops_between(current, new_label)
            return state.with_value(name, new_label), name, applied
    return state, None, 0.0


def target_error_ev(metrics: FrameMetrics, target_headroom_stops: float) -> float:
    """Stops of light to ADD so the highlights sit at the headroom target.

    Positive means the frame is darker than the target (needs more light);
    negative means it is brighter (needs less light).
    """
    return -target_headroom_stops - metrics.highlight_ev


class RampingEngine:
    """Stateful engine that turns frame metrics into ramping decisions."""

    def __init__(
        self,
        params: RampParams,
        location: LocationConfig,
        *,
        aperture_labels: list[str] | None = None,
        aperture_axis: ExposureAxis | None = None,
    ) -> None:
        if aperture_axis is not None:
            self.aperture_axis = aperture_axis
        elif aperture_labels is not None:
            self.aperture_axis = build_aperture_axis(aperture_labels)
        else:
            self.aperture_axis = APERTURE_AXIS
        params.validate_aperture_limits(self.aperture_axis)
        self.params = params
        self.location = location
        # Signed exposure errors (+ve == need more light) for trend detection.
        self._error_history: deque[float] = deque(maxlen=params.trend_window)
        # Mean EV history for proactive dawn detection (scene brightening rate).
        self._mean_ev_history: deque[float] = deque(
            maxlen=params.dawn_proactive_mean_ev_window
        )

    def _target_error(self, metrics: FrameMetrics, headroom_stops: float) -> float:
        """Stops of light to ADD to reach the headroom target (+ve == add)."""
        return target_error_ev(metrics, headroom_stops)

    def _direction_allowed(
        self, direction: LightDirection, phase: Phase, *, is_override: bool
    ) -> bool:
        """Phase gate that prevents macro back-and-forth ramping."""
        if phase is Phase.DARKENING:
            # Evening: free to add light; only remove light to fight white clipping.
            return direction is LightDirection.ADD or is_override
        # Morning: free to remove light; only add light to rescue black clipping.
        return direction is LightDirection.REMOVE or is_override

    def _trend_active(self, direction: LightDirection) -> bool:
        """True if every recent frame error is large and in ``direction``."""
        if len(self._error_history) < self.params.trend_window:
            return False
        threshold = self.params.trend_ev_threshold
        if direction is LightDirection.ADD:
            return all(e > threshold for e in self._error_history)
        return all(e < -threshold for e in self._error_history)

    def _mean_ev_rising(self, metrics: FrameMetrics) -> bool:
        """True if the current mean_ev is above the running average.

        This detects that the scene is brightening faster than its recent
        history — the key leading indicator of imminent white clipping at dawn.
        Requires a full window of history to avoid false positives on the first
        few frames.
        """
        window = self.params.dawn_proactive_mean_ev_window
        if len(self._mean_ev_history) < window:
            return False
        avg = sum(self._mean_ev_history) / len(self._mean_ev_history)
        return metrics.mean_ev > avg

    def decide(
        self, state: ExposureState, metrics: FrameMetrics, now: datetime
    ) -> RampDecision:
        """Decide the ramp for one analysed frame."""
        params = self.params
        phase = phase_at(self.location, now)
        # Day vs night tolerances blend smoothly with the sun's altitude so the
        # exposure target never jumps at the day/night boundary.
        night_fraction = params.night_fraction(sun_altitude(self.location, now))
        headroom = params.effective_headroom_stops(night_fraction)
        white_clip_threshold = params.effective_white_clip_pct_threshold(night_fraction)
        error_ev = self._target_error(metrics, headroom)
        self._error_history.append(error_ev)
        self._mean_ev_history.append(metrics.mean_ev)

        # 1) Clipping protection (ETTR first priority), in priority order:
        #    a. Black-clip *limit* - too much of the frame is crushed to black.
        #       Rescuing the shadows beats everything else (it is what stops night
        #       frames from being lifted into noise in post), so it overrides the
        #       phase gate, the dead-band AND the white-clip protection below.
        #    b. White clip - blown highlights force the image darker. The
        #       tolerance is relaxed at night so small light sources may clip.
        #    c. Otherwise track the (day/night-blended) ETTR target; black
        #       clipping above the softer threshold lets us brighten past the
        #       dead-band when the highlights are also below target.
        black_over_limit = metrics.clipping_black_pct > params.black_clip_pct_limit
        white_clip = metrics.clipping_white_pct > white_clip_threshold
        black_clip = metrics.clipping_black_pct > params.black_clip_pct_threshold

        if black_over_limit:
            direction = LightDirection.ADD
            is_override = True
            reason = (
                f"black-clip limit ({metrics.clipping_black_pct:.1f}% > "
                f"{params.black_clip_pct_limit:.0f}%)"
            )
        elif white_clip:
            direction = LightDirection.REMOVE
            is_override = True
            reason = f"white-clip override ({metrics.clipping_white_pct:.2f}%)"
        elif (
            phase is Phase.BRIGHTENING
            and night_fraction > 0  # not full daylight yet
            and metrics.clipping_black_pct < params.dawn_proactive_black_pct
            and self._mean_ev_rising(metrics)
        ):
            # Proactive dawn ramping: the scene is brightening (mean_ev is above
            # its recent running average) and shadows have filled in (black% is
            # low). Remove light now to stay ahead of the sunrise curve before
            # white clipping fires. The night_fraction>0 guard ensures this
            # deactivates in full daylight, leaving normal ETTR in charge.
            direction = LightDirection.REMOVE
            is_override = True  # override deadband — we must act proactively
            reason = (
                f"dawn proactive (black {metrics.clipping_black_pct:.1f}% < "
                f"{params.dawn_proactive_black_pct:.0f}%)"
            )
        else:
            within_deadband = abs(error_ev) <= params.deadband_stops
            if within_deadband and not (black_clip and error_ev > 0):
                return RampDecision.no_change(
                    state, error_ev, phase, "within deadband"
                )
            direction = LightDirection.ADD if error_ev > 0 else LightDirection.REMOVE
            is_override = False
            reason = "exposure target"
            if black_clip and direction is LightDirection.ADD:
                reason = f"exposure target (black-clip {metrics.clipping_black_pct:.1f}%)"

        # 2) Phase gate to avoid ramping back and forth across the day.
        if not self._direction_allowed(direction, phase, is_override=is_override):
            return RampDecision.no_change(
                state,
                error_ev,
                phase,
                f"{direction.name.lower()} suppressed in {phase.value} phase",
            )

        # 3) Trend anticipation: skip a step when persistently >1 EV behind.
        steps = 2 if self._trend_active(direction) else 1

        # 4) Move the highest-priority axis that still has room.
        #    For white-clip overrides, use the severity-scaled shutter-priority
        #    function so we can shed light fast enough at dawn.
        if white_clip:
            new_state, param, applied = apply_white_clip_remove(
                state,
                metrics.clipping_white_pct,
                params,
                aperture_axis=self.aperture_axis,
            )
        else:
            new_state, param, applied = apply_light_change(
                state,
                direction,
                steps,
                params,
                aperture_axis=self.aperture_axis,
            )
        if param is None:
            return RampDecision.no_change(
                state,
                error_ev,
                phase,
                f"{direction.name.lower()} limit reached",
            )

        return RampDecision(
            changed=True,
            old_state=state,
            new_state=new_state,
            error_ev=error_ev,
            phase=phase,
            reason=reason,
            param=param,
            direction=direction,
            steps=steps,
            applied_stops=applied,
        )
