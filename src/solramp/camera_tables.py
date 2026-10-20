# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canon EOS 5D Mark IV exposure setting tables and brightness-stop math.

Everything in this module is *camera specific*: the exact ISO, aperture and
shutter-speed choices below are the ones the 5D Mark IV exposes over PTP.  To
support a different body, replace the three ``*_LABELS`` lists - see the README
section "Hardcoded for the Canon 5D Mark IV".

We model every setting as a contribution to overall image *brightness* measured
in stops (powers of two of light).  Increasing a value's ``stops`` by 1 doubles
the light reaching the sensor:

* ISO       - ``log2(iso / 100)``          (higher ISO  -> brighter)
* shutter   - ``log2(exposure_seconds)``   (longer time -> brighter)
* aperture  - ``-2 * log2(f_number)``      (wider f-stop -> brighter)

The absolute image brightness equals the (scene-dependent) luminance plus the
sum of these three contributions, so a *change* in image brightness caused by a
settings change is simply the change in the summed stops.  Working in this
single linear "stops" space keeps the ramping engine simple and camera-agnostic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Raw camera choice labels (exact strings reported / accepted by gphoto2).
# Order matches the camera's choice list in the appendix.  "Auto" is removed
# from the ISO list on purpose: this app never uses Auto for anything.
# ---------------------------------------------------------------------------
ISO_LABELS: list[str] = [
    "100", "125", "160", "200", "250", "320", "400", "500", "640", "800",
    "1000", "1250", "1600", "2000", "2500", "3200", "4000", "5000", "6400",
    "8000", "10000", "12800", "16000", "20000", "25600", "32000",
]

APERTURE_LABELS: list[str] = [
    "2.8", "3.2", "3.5", "4", "4.5", "5", "5.6", "6.3", "7.1", "8", "9", "10",
    "11", "13", "14", "16", "18", "20", "22",
]

# Slow (bright) -> fast (dark).  Strings such as "10.3" / "0.6" are whole-second
# exposures; "1/250" style strings are fractional-second exposures.
SHUTTER_LABELS: list[str] = [
    "30", "25", "20", "15", "13", "10.3", "8", "6.3", "5", "4", "3.2", "2.5",
    "2", "1.6", "1.3", "1", "0.8", "0.6", "0.5", "0.4", "0.3",
    "1/4", "1/5", "1/6", "1/8", "1/10", "1/13", "1/15", "1/20", "1/25", "1/30",
    "1/40", "1/50", "1/60", "1/80", "1/100", "1/125", "1/160", "1/200", "1/250",
    "1/320", "1/400", "1/500", "1/640", "1/800", "1/1000", "1/1250", "1/1600",
    "1/2000", "1/2500", "1/3200", "1/4000", "1/5000", "1/6400", "1/8000",
]


def shutter_label_to_seconds(label: str) -> float:
    """Return the exposure time in seconds for a 5D Mark IV shutter label.

    >>> shutter_label_to_seconds("1/250")
    0.004
    >>> shutter_label_to_seconds("0.6")
    0.6
    >>> shutter_label_to_seconds("30")
    30.0
    """
    label = label.strip()
    if "/" in label:
        numerator, denominator = label.split("/", 1)
        return float(numerator) / float(denominator)
    return float(label)


@dataclass(frozen=True)
class SettingValue:
    """A single selectable value on one exposure axis."""

    label: str  # exact string sent to / logged for the camera
    stops: float  # brightness contribution (higher == more light)


class ExposureAxis:
    """One exposure dimension (ISO, aperture or shutter).

    Values are stored ordered from *darkest* to *brightest* so that "make the
    image brighter" always means "move to a higher index", regardless of how
    the camera happens to order its native choice list.
    """

    def __init__(self, name: str, values: list[SettingValue]) -> None:
        self.name = name
        # Sort dark -> bright. Stable so equal-stop entries keep input order.
        self._values: list[SettingValue] = sorted(values, key=lambda v: v.stops)
        self._index_by_label: dict[str, int] = {
            v.label: i for i, v in enumerate(self._values)
        }
        if len(self._index_by_label) != len(self._values):
            raise ValueError(f"duplicate labels on axis {name!r}")

    # -- lookups ---------------------------------------------------------
    @property
    def labels(self) -> list[str]:
        """All labels ordered dark -> bright."""
        return [v.label for v in self._values]

    def __len__(self) -> int:
        return len(self._values)

    def index_of(self, label: str) -> int:
        try:
            return self._index_by_label[label]
        except KeyError as exc:
            raise KeyError(
                f"{label!r} is not a valid {self.name} value"
            ) from exc

    def stops_of(self, label: str) -> float:
        return self._values[self.index_of(label)].stops

    def label_at(self, index: int) -> str:
        return self._values[index].label

    def is_valid(self, label: str) -> bool:
        return label in self._index_by_label

    # -- stepping --------------------------------------------------------
    def stops_between(self, from_label: str, to_label: str) -> float:
        """Brightness change (stops) going ``from_label`` -> ``to_label``.

        Positive means the destination is brighter (more light).
        """
        return self.stops_of(to_label) - self.stops_of(from_label)

    def step(
        self,
        label: str,
        steps: int,
        *,
        min_label: str | None = None,
        max_label: str | None = None,
    ) -> str:
        """Move ``steps`` indices brighter (``steps`` > 0) or darker (< 0).

        The result is clamped to the axis bounds and to the optional
        ``min_label`` (darkest allowed) / ``max_label`` (brightest allowed)
        limits.  ``min_label``/``max_label`` are interpreted in *brightness*
        order, not in the camera's native label order.
        """
        low = 0
        high = len(self._values) - 1
        if min_label is not None:
            low = max(low, self.index_of(min_label))
        if max_label is not None:
            high = min(high, self.index_of(max_label))
        if low > high:
            raise ValueError(
                f"empty allowed range on axis {self.name}: "
                f"min={min_label} max={max_label}"
            )
        target = self.index_of(label) + steps
        clamped = min(high, max(low, target))
        return self._values[clamped].label

    def can_step(
        self,
        label: str,
        steps: int,
        *,
        min_label: str | None = None,
        max_label: str | None = None,
    ) -> bool:
        """True if stepping actually moves (i.e. we are not already clamped)."""
        return self.step(
            label, steps, min_label=min_label, max_label=max_label
        ) != label


def _build_iso_axis() -> ExposureAxis:
    return ExposureAxis(
        "iso",
        [SettingValue(label, math.log2(int(label) / 100.0)) for label in ISO_LABELS],
    )


def build_aperture_axis(labels: list[str]) -> ExposureAxis:
    """Build an aperture axis from camera-reported f-number labels."""
    return ExposureAxis(
        "aperture",
        [
            SettingValue(label, -2.0 * math.log2(float(label)))
            for label in labels
        ],
    )


def _build_aperture_axis() -> ExposureAxis:
    return build_aperture_axis(APERTURE_LABELS)


def _build_shutter_axis() -> ExposureAxis:
    return ExposureAxis(
        "shutter",
        [
            SettingValue(label, math.log2(shutter_label_to_seconds(label)))
            for label in SHUTTER_LABELS
        ],
    )


# Singleton axes (the tables are immutable, so module-level instances are fine).
ISO_AXIS = _build_iso_axis()
APERTURE_AXIS = _build_aperture_axis()
SHUTTER_AXIS = _build_shutter_axis()


def fastest_shutter_within(max_seconds: float) -> str:
    """Return the brightest (slowest) shutter label whose time <= max_seconds.

    Used to cap the night-time exposure at ``interval - margin`` seconds.  If no
    shutter is short enough (max_seconds below the fastest shutter) the fastest
    available shutter is returned.
    """
    best: str | None = None
    best_seconds = -1.0
    for label in SHUTTER_LABELS:
        seconds = shutter_label_to_seconds(label)
        if seconds <= max_seconds and seconds > best_seconds:
            best_seconds = seconds
            best = label
    if best is None:
        # max_seconds is below even 1/8000s; return the fastest shutter.
        return SHUTTER_AXIS.label_at(0)
    return best
