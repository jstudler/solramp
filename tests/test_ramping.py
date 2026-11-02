# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the exposure ramping decision engine."""

from __future__ import annotations

from datetime import datetime

import pytest

from solramp import ramping
from solramp.camera_tables import build_aperture_axis
from solramp.astral_phase import LocationConfig, Phase
from solramp.ramping import (
    ExposureState,
    LightDirection,
    RampParams,
    RampingEngine,
    apply_light_change,
    normalize_aperture_limits,
    target_error_ev,
)

from .conftest import make_metrics

NOW = datetime(2024, 6, 21, 18, 0)
MID_STATE = ExposureState(iso="800", aperture="8", shutter="1/125")


@pytest.fixture
def params() -> RampParams:
    return RampParams()


@pytest.fixture
def engine(params: RampParams) -> RampingEngine:
    return RampingEngine(params, LocationConfig())


@pytest.fixture
def force_phase(monkeypatch: pytest.MonkeyPatch):
    """Return a setter that pins the engine's detected phase."""

    def _set(phase: Phase) -> None:
        monkeypatch.setattr(ramping, "phase_at", lambda loc, now: phase)

    return _set


@pytest.fixture
def force_altitude(monkeypatch: pytest.MonkeyPatch):
    """Return a setter that pins the sun altitude the engine sees (degrees)."""

    def _set(altitude_deg: float) -> None:
        monkeypatch.setattr(ramping, "sun_altitude", lambda loc, now: altitude_deg)

    return _set


# --------------------------------------------------------------------------
# target_error_ev
# --------------------------------------------------------------------------
def test_target_error_positive_when_too_dark() -> None:
    # highlights 3 stops under saturation, want 1 stop headroom -> add 2 stops.
    assert target_error_ev(make_metrics(highlight_ev=-3.0), 1.0) == pytest.approx(2.0)


def test_target_error_negative_when_too_bright() -> None:
    assert target_error_ev(make_metrics(highlight_ev=0.0), 1.0) == pytest.approx(-1.0)


# --------------------------------------------------------------------------
# apply_light_change priority order
# --------------------------------------------------------------------------
def test_add_light_moves_aperture_first(params: RampParams) -> None:
    state, axis, applied = apply_light_change(MID_STATE, LightDirection.ADD, 1, params)
    assert axis == "aperture"
    assert applied > 0  # brighter
    assert state.aperture != MID_STATE.aperture


def test_add_light_falls_through_to_shutter_when_aperture_maxed(
    params: RampParams,
) -> None:
    wide_open = ExposureState(iso="800", aperture="2.8", shutter="1/125")
    state, axis, _ = apply_light_change(wide_open, LightDirection.ADD, 1, params)
    assert axis == "shutter"
    assert state.shutter != "1/125"


def test_add_light_falls_through_to_iso_last(params: RampParams) -> None:
    near_max = ExposureState(iso="800", aperture="2.8", shutter="30")
    state, axis, _ = apply_light_change(near_max, LightDirection.ADD, 1, params)
    assert axis == "iso"
    assert state.iso != "800"


def test_remove_light_moves_iso_first(params: RampParams) -> None:
    state, axis, applied = apply_light_change(MID_STATE, LightDirection.REMOVE, 1, params)
    assert axis == "iso"
    assert applied < 0  # darker
    assert state.iso != MID_STATE.iso


def test_apply_light_returns_none_at_all_limits(params: RampParams) -> None:
    maxed = ExposureState(iso="32000", aperture="2.8", shutter="30")
    state, axis, applied = apply_light_change(maxed, LightDirection.ADD, 1, params)
    assert axis is None
    assert state == maxed
    assert applied == 0.0


# --------------------------------------------------------------------------
# decide(): direction, phase gating, clipping overrides
# --------------------------------------------------------------------------
def test_darkening_too_dark_adds_light(engine: RampingEngine, force_phase) -> None:
    force_phase(Phase.DARKENING)
    decision = engine.decide(MID_STATE, make_metrics(highlight_ev=-3.0), NOW)
    assert decision.changed
    assert decision.direction is LightDirection.ADD
    assert decision.param == "aperture"


def test_brightening_too_bright_removes_light(engine: RampingEngine, force_phase) -> None:
    force_phase(Phase.BRIGHTENING)
    decision = engine.decide(MID_STATE, make_metrics(highlight_ev=0.5), NOW)
    assert decision.changed
    assert decision.direction is LightDirection.REMOVE
    assert decision.param == "iso"


def test_add_suppressed_in_brightening_phase(engine: RampingEngine, force_phase) -> None:
    force_phase(Phase.BRIGHTENING)
    decision = engine.decide(MID_STATE, make_metrics(highlight_ev=-3.0), NOW)
    assert not decision.changed
    assert "suppressed" in decision.reason


def test_remove_suppressed_in_darkening_phase(engine: RampingEngine, force_phase) -> None:
    force_phase(Phase.DARKENING)
    # Too bright but no white clip -> would remove light, which darkening forbids.
    decision = engine.decide(MID_STATE, make_metrics(highlight_ev=0.5), NOW)
    assert not decision.changed
    assert "suppressed" in decision.reason


def test_within_deadband_holds(engine: RampingEngine, force_phase) -> None:
    force_phase(Phase.DARKENING)
    # error == +0.2 stop, inside the 0.33 dead-band.
    decision = engine.decide(MID_STATE, make_metrics(highlight_ev=-1.2), NOW)
    assert not decision.changed
    assert "deadband" in decision.reason


def test_white_clip_overrides_phase_and_removes_light(
    engine: RampingEngine, force_phase
) -> None:
    force_phase(Phase.DARKENING)  # darkening normally forbids removing light
    decision = engine.decide(
        MID_STATE, make_metrics(highlight_ev=-0.5, white=5.0), NOW
    )
    assert decision.changed
    assert decision.direction is LightDirection.REMOVE
    assert "white-clip" in decision.reason


def test_black_clip_bypasses_deadband_when_dark(
    engine: RampingEngine, force_phase
) -> None:
    force_phase(Phase.DARKENING)
    # error == +0.30 stop (inside dead-band) but heavy black clipping + darkening.
    dark_metrics = make_metrics(highlight_ev=-1.3, black=30.0)
    decision = engine.decide(MID_STATE, dark_metrics, NOW)
    assert decision.changed
    assert decision.direction is LightDirection.ADD
    assert "black-clip" in decision.reason


def test_black_clip_below_threshold_still_holds(
    engine: RampingEngine, force_phase
) -> None:
    force_phase(Phase.DARKENING)
    decision = engine.decide(MID_STATE, make_metrics(highlight_ev=-1.3, black=1.0), NOW)
    assert not decision.changed


def test_limit_reached_holds(engine: RampingEngine, force_phase) -> None:
    force_phase(Phase.DARKENING)
    maxed = ExposureState(iso="32000", aperture="2.8", shutter="30")
    decision = engine.decide(maxed, make_metrics(highlight_ev=-5.0), NOW)
    assert not decision.changed
    assert "limit" in decision.reason


def test_sustained_trend_triggers_two_step_skip(
    engine: RampingEngine, force_phase
) -> None:
    force_phase(Phase.DARKENING)
    metrics = make_metrics(highlight_ev=-4.0)  # error == +3 EV, well over 1 EV
    first = engine.decide(MID_STATE, metrics, NOW)
    engine.decide(MID_STATE, metrics, NOW)
    third = engine.decide(MID_STATE, metrics, NOW)
    assert first.steps == 1
    assert third.steps == 2  # trend window of 3 confirmed -> skip a step


def test_decision_describe_is_human_readable(engine: RampingEngine, force_phase) -> None:
    force_phase(Phase.DARKENING)
    decision = engine.decide(MID_STATE, make_metrics(highlight_ev=-3.0), NOW)
    text = decision.describe()
    assert "aperture" in text and "EV" in text


# --------------------------------------------------------------------------
# Day vs night tolerance blend (RampParams helpers)
# --------------------------------------------------------------------------
def test_night_fraction_extremes_and_midpoint(params: RampParams) -> None:
    assert params.night_fraction(10.0) == 0.0  # well above the day altitude
    assert params.night_fraction(-10.0) == 1.0  # well below the night altitude
    # Default band is +6 .. -6, so altitude 0 is exactly halfway.
    assert params.night_fraction(0.0) == pytest.approx(0.5)


def test_effective_headroom_blends_day_to_night(params: RampParams) -> None:
    assert params.effective_headroom_stops(0.0) == pytest.approx(params.target_headroom_stops)
    assert params.effective_headroom_stops(1.0) == pytest.approx(params.target_headroom_stops_night)
    assert params.effective_headroom_stops(0.5) == pytest.approx(
        (params.target_headroom_stops + params.target_headroom_stops_night) / 2
    )


def test_effective_white_clip_threshold_blends_day_to_night(params: RampParams) -> None:
    assert params.effective_white_clip_pct_threshold(0.0) == pytest.approx(
        params.white_clip_pct_threshold
    )
    assert params.effective_white_clip_pct_threshold(1.0) == pytest.approx(
        params.white_clip_pct_threshold_night
    )


def test_blend_is_monotonic_for_smooth_transition(params: RampParams) -> None:
    # As the sun drops, the night fraction must rise monotonically and the
    # headroom fall monotonically (brighter) - a smooth, jump-free transition.
    altitudes = [10.0, 6.0, 3.0, 0.0, -3.0, -6.0, -10.0]
    fractions = [params.night_fraction(a) for a in altitudes]
    headrooms = [params.effective_headroom_stops(f) for f in fractions]
    assert fractions == sorted(fractions)  # non-decreasing as it gets darker
    assert headrooms == sorted(headrooms, reverse=True)  # non-increasing


def test_invalid_altitude_band_rejected() -> None:
    with pytest.raises(ValueError, match="night_altitude_deg"):
        RampParams(day_altitude_deg=-6.0, night_altitude_deg=6.0)


def test_normalize_aperture_limits_falls_back_to_full_lens_range() -> None:
    axis = build_aperture_axis(["16", "11", "8", "5.6", "4", "2.8"])
    params = RampParams(aperture_dark_limit="22", aperture_bright_limit="2.8")
    normalized, used_fallback = normalize_aperture_limits(params, axis)
    assert used_fallback is True
    assert normalized.aperture_dark_limit == "16"
    assert normalized.aperture_bright_limit == "2.8"


def test_engine_uses_runtime_aperture_axis_for_limits(force_phase) -> None:
    force_phase(Phase.DARKENING)
    axis = build_aperture_axis(["8", "5.6", "4"])
    params = RampParams(aperture_dark_limit="8", aperture_bright_limit="4")
    engine = RampingEngine(params, LocationConfig(), aperture_axis=axis)
    state = ExposureState(iso="800", aperture="4", shutter="1/125")
    decision = engine.decide(state, make_metrics(highlight_ev=-3.0), NOW)
    assert decision.changed
    assert decision.param == "shutter"


# --------------------------------------------------------------------------
# decide(): night exposes brighter and tolerates more white clipping
# --------------------------------------------------------------------------
def test_night_exposes_brighter_than_day(
    engine: RampingEngine, force_phase, force_altitude
) -> None:
    force_phase(Phase.DARKENING)
    # Highlights 0.5 stop below clipping. At night (headroom 0) the target is
    # clipping itself, so this frame is too dark and the engine adds light.
    force_altitude(-10.0)
    night = engine.decide(MID_STATE, make_metrics(highlight_ev=-0.5), NOW)
    assert night.changed
    assert night.direction is LightDirection.ADD


def test_day_holds_where_night_would_brighten(
    engine: RampingEngine, force_phase, force_altitude
) -> None:
    force_phase(Phase.DARKENING)
    # Same frame in daylight (headroom 1.0): error is -0.5, i.e. slightly bright;
    # darkening forbids removing light, so it holds rather than brightening.
    force_altitude(10.0)
    day = engine.decide(MID_STATE, make_metrics(highlight_ev=-0.5), NOW)
    assert not day.changed


def test_white_clip_tolerated_at_night_but_not_day(
    engine: RampingEngine, force_phase, force_altitude
) -> None:
    force_phase(Phase.DARKENING)
    # 2% white clip sits between the day (0.5%) and night (5%) thresholds.
    metrics = make_metrics(highlight_ev=0.0, white=2.0)

    force_altitude(10.0)  # day: 2% > 0.5% -> forced darkening
    day = engine.decide(MID_STATE, metrics, NOW)
    assert day.changed
    assert day.direction is LightDirection.REMOVE
    assert "white-clip" in day.reason

    force_altitude(-10.0)  # night: 2% < 5% -> tolerated, target met -> hold
    night = engine.decide(MID_STATE, metrics, NOW)
    assert not night.changed


# --------------------------------------------------------------------------
# decide(): hard black-clip ceiling (70%) outranks phase and white clip
# --------------------------------------------------------------------------
def test_black_clip_limit_forces_add_overriding_phase(
    engine: RampingEngine, force_phase, force_altitude
) -> None:
    force_phase(Phase.BRIGHTENING)  # normally only allows removing light
    force_altitude(-10.0)
    metrics = make_metrics(highlight_ev=0.0, black=75.0)
    decision = engine.decide(MID_STATE, metrics, NOW)
    assert decision.changed
    assert decision.direction is LightDirection.ADD
    assert "black-clip limit" in decision.reason


def test_black_clip_limit_outranks_white_clip(
    engine: RampingEngine, force_phase, force_altitude
) -> None:
    force_phase(Phase.DARKENING)
    force_altitude(-10.0)
    # Both clip at once: keeping the blacks wins over a clean highlight.
    metrics = make_metrics(highlight_ev=0.0, white=10.0, black=80.0)
    decision = engine.decide(MID_STATE, metrics, NOW)
    assert decision.changed
    assert decision.direction is LightDirection.ADD
    assert "black-clip limit" in decision.reason


def test_white_clip_still_removes_when_black_below_limit(
    engine: RampingEngine, force_phase, force_altitude
) -> None:
    force_phase(Phase.DARKENING)
    force_altitude(-10.0)
    # Black 50% is under the 70% ceiling, so the night white-clip protection
    # (5%) still applies: 10% white clip forces darkening.
    metrics = make_metrics(highlight_ev=0.0, white=10.0, black=50.0)
    decision = engine.decide(MID_STATE, metrics, NOW)
    assert decision.changed
    assert decision.direction is LightDirection.REMOVE
    assert "white-clip" in decision.reason


# --------------------------------------------------------------------------
# White-clip severity-scaled response (Fix C)
# --------------------------------------------------------------------------
def test_white_clip_mild_takes_1_step(
    engine: RampingEngine, force_phase
) -> None:
    """White clip at 2% (above 0.5% day threshold, below 5% severe) -> 1 step."""
    force_phase(Phase.DARKENING)
    from solramp.ramping import apply_white_clip_remove
    # Mild clip: 1 step, normal iso-first order.
    new_state, param, applied = apply_white_clip_remove(MID_STATE, 2.0, engine.params)
    assert param == "iso"
    # 1 step = 1/3 stop on the ISO axis
    assert 0.3 < abs(applied) < 0.4


def test_white_clip_severe_takes_multi_step(
    engine: RampingEngine, force_phase
) -> None:
    """White clip >5% should take 2 steps via the severity function."""
    force_phase(Phase.DARKENING)
    from solramp.ramping import apply_white_clip_remove
    new_state, param, applied = apply_white_clip_remove(MID_STATE, 10.0, engine.params)
    assert param == "iso"  # normal remove order: iso first
    assert abs(applied) > 0.5  # 2 steps = ~2/3 stop


def test_white_clip_critical_uses_shutter_priority(
    engine: RampingEngine, force_phase
) -> None:
    """White clip >20% at dawn (shutter=25s) should attack shutter first."""
    force_phase(Phase.BRIGHTENING)
    # Dawn scenario: already at ISO 100, shutter 25s.
    dawn_state = ExposureState(iso="100", aperture="2.8", shutter="25")
    decision = engine.decide(dawn_state, make_metrics(highlight_ev=0.0, white=30.0), NOW)
    assert decision.changed
    assert decision.direction is LightDirection.REMOVE
    # At >20% the shutter-priority path is used: attacks shutter, not ISO.
    assert decision.param == "shutter"


def test_white_clip_critical_fallback_to_single_step(params: RampParams) -> None:
    """When ISO near limit, critical clip still removes *something*."""
    from solramp.ramping import apply_white_clip_remove
    # ISO 125 has only 1 step to 100 (can't do 3). Shutter at 1/8000 (fastest).
    # Aperture at 8. The function should fall back to whatever it can step.
    state = ExposureState(iso="125", aperture="8", shutter="1/8000")
    new_state, param, applied = apply_white_clip_remove(state, 25.0, params)
    # It should still find some axis to step (iso can do 1 step to 100).
    assert param is not None
    assert applied != 0.0


# --------------------------------------------------------------------------
# Proactive dawn ramping
# --------------------------------------------------------------------------
class TestProactiveDawnRamping:
    """When brightening with mean_ev rising above its recent average and
    black-clip has dropped below the threshold, the engine proactively removes
    light before white clipping fires."""

    @staticmethod
    def _prefill_mean_ev(engine: RampingEngine, values: list[float]) -> None:
        """Seed the engine's mean_ev history with prior frame values."""
        for v in values:
            engine._mean_ev_history.append(v)

    def test_proactive_fires_when_mean_ev_rising_and_black_low(
        self, engine: RampingEngine, force_phase, force_altitude
    ) -> None:
        """mean_ev above running avg + black < 10% + brightening -> remove."""
        force_phase(Phase.BRIGHTENING)
        force_altitude(0.0)  # night_fraction > 0
        # History: scene has been at mean_ev ~ -3.5, now jumps to -3.0
        self._prefill_mean_ev(engine, [-3.8, -3.7, -3.6, -3.5, -3.4])
        metrics = make_metrics(highlight_ev=-1.2, black=3.0, white=0.5, mean_ev=-3.0)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert decision.changed
        assert decision.direction is LightDirection.REMOVE
        assert "dawn proactive" in decision.reason

    def test_proactive_fires_at_deep_night_altitude(
        self, engine: RampingEngine, force_phase, force_altitude
    ) -> None:
        """Sun at -9° (below old -6° gate) still triggers proactive."""
        force_phase(Phase.BRIGHTENING)
        force_altitude(-9.0)  # night_fraction=1.0 but still > 0
        self._prefill_mean_ev(engine, [-3.8, -3.7, -3.6, -3.5, -3.4])
        metrics = make_metrics(highlight_ev=-1.2, black=5.0, white=0.5, mean_ev=-3.0)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert decision.changed
        assert decision.direction is LightDirection.REMOVE
        assert "dawn proactive" in decision.reason

    def test_proactive_does_not_fire_when_mean_ev_not_rising(
        self, engine: RampingEngine, force_phase, force_altitude
    ) -> None:
        """mean_ev below or at running average -> no proactive."""
        force_phase(Phase.BRIGHTENING)
        force_altitude(0.0)
        # History rising, but current is below the average (e.g. after a ramp)
        self._prefill_mean_ev(engine, [-3.0, -2.9, -2.8, -2.7, -2.6])
        # running average is -2.8
        metrics = make_metrics(highlight_ev=-0.6, black=3.0, white=0.3, mean_ev=-3.0)
        decision = engine.decide(MID_STATE, metrics, NOW)
        # mean_ev=-3.0 < avg=-2.8 -> not rising -> no proactive
        assert not decision.changed

    def test_proactive_does_not_fire_without_enough_history(
        self, engine: RampingEngine, force_phase, force_altitude
    ) -> None:
        """Need a full window of history before triggering."""
        force_phase(Phase.BRIGHTENING)
        force_altitude(0.0)
        # Only 3 frames of history (window is 5)
        self._prefill_mean_ev(engine, [-3.5, -3.4, -3.3])
        metrics = make_metrics(highlight_ev=-0.6, black=3.0, white=0.3, mean_ev=-3.0)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert not decision.changed

    def test_proactive_does_not_fire_when_black_above_threshold(
        self, engine: RampingEngine, force_phase, force_altitude
    ) -> None:
        """Black >= 10% -> no proactive action (scene still dark enough)."""
        force_phase(Phase.BRIGHTENING)
        force_altitude(0.0)
        self._prefill_mean_ev(engine, [-3.8, -3.7, -3.6, -3.5, -3.4])
        metrics = make_metrics(highlight_ev=-0.6, black=15.0, white=0.3, mean_ev=-3.0)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert not decision.changed

    def test_proactive_does_not_fire_in_full_daylight(
        self, engine: RampingEngine, force_phase, force_altitude
    ) -> None:
        """Sun above day_altitude_deg -> night_fraction=0, no proactive."""
        force_phase(Phase.BRIGHTENING)
        force_altitude(10.0)  # above 6° -> full daylight, night_fraction=0
        self._prefill_mean_ev(engine, [-3.8, -3.7, -3.6, -3.5, -3.4])
        metrics = make_metrics(highlight_ev=-1.2, black=3.0, white=0.3, mean_ev=-3.0)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert not decision.changed

    def test_proactive_does_not_fire_during_darkening(
        self, engine: RampingEngine, force_phase, force_altitude
    ) -> None:
        """Even with rising mean_ev + low black in twilight, darkening blocks."""
        force_phase(Phase.DARKENING)
        force_altitude(0.0)
        self._prefill_mean_ev(engine, [-3.8, -3.7, -3.6, -3.5, -3.4])
        metrics = make_metrics(highlight_ev=-0.6, black=3.0, white=0.3, mean_ev=-3.0)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert not decision.changed

    def test_white_clip_takes_priority_over_proactive(
        self, engine: RampingEngine, force_phase, force_altitude
    ) -> None:
        """When white-clip is already triggered, that path runs instead."""
        force_phase(Phase.BRIGHTENING)
        force_altitude(0.0)
        self._prefill_mean_ev(engine, [-3.8, -3.7, -3.6, -3.5, -3.4])
        metrics = make_metrics(highlight_ev=-0.5, black=2.0, white=5.0, mean_ev=-3.0)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert decision.changed
        assert decision.direction is LightDirection.REMOVE
        assert "white-clip" in decision.reason  # not "dawn proactive"

    def test_proactive_respects_custom_threshold(
        self, force_phase, force_altitude
    ) -> None:
        """dawn_proactive_black_pct can be tuned via params."""
        params = RampParams(dawn_proactive_black_pct=5.0)
        engine = RampingEngine(params, LocationConfig())
        force_phase(Phase.BRIGHTENING)
        force_altitude(0.0)
        self._prefill_mean_ev(engine, [-3.8, -3.7, -3.6, -3.5, -3.4])
        # black=7% is above the custom 5% threshold -> no proactive
        metrics = make_metrics(highlight_ev=-1.2, black=7.0, white=0.3, mean_ev=-3.0)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert not decision.changed
        # black=3% is below -> proactive fires
        metrics = make_metrics(highlight_ev=-1.2, black=3.0, white=0.3, mean_ev=-2.9)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert decision.changed
        assert "dawn proactive" in decision.reason

    def test_proactive_naturally_stops_after_removal(
        self, engine: RampingEngine, force_phase, force_altitude
    ) -> None:
        """After a ramp removal makes the image darker, mean_ev drops below
        the running average and proactive stops firing (anti-flap)."""
        force_phase(Phase.BRIGHTENING)
        force_altitude(0.0)
        # Simulate 5 frames of rising mean_ev
        self._prefill_mean_ev(engine, [-3.4, -3.3, -3.2, -3.1, -3.0])
        # Current frame is above average -> proactive fires
        metrics = make_metrics(highlight_ev=-1.2, black=3.0, white=0.5, mean_ev=-2.8)
        decision = engine.decide(MID_STATE, metrics, NOW)
        assert decision.changed
        assert "dawn proactive" in decision.reason
        # After removal: next frame mean_ev drops (exposure reduced)
        # History is now [-3.3, -3.2, -3.1, -3.0, -2.8], avg=-3.08
        # New mean_ev=-3.5 (much darker due to ramp) < avg -> no trigger
        metrics2 = make_metrics(highlight_ev=-0.6, black=4.0, white=0.2, mean_ev=-3.5)
        decision2 = engine.decide(MID_STATE, metrics2, NOW)
        assert not decision2.changed

