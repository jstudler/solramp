# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate tiny synthetic raw files for the test-suite and benchmarks.

The analyser (:mod:`solramp.raw_analysis`) is the one place that touches
rawpy/libraw, so testing it honestly needs a file that libraw really decodes --
mocking ``rawpy.imread`` would only assert that we call our own mock.  Shipping
real camera CR2s costs ~31 MB each, so instead we *build* a raw file here.

libraw reads uncompressed DNG, and DNG is just TIFF with a few extra tags, so a
valid one can be written with :mod:`struct` alone.  The result is ~12 kB for the
default size and decodes through the exact ``postprocess()`` call the app uses.

Two properties make these better fixtures than the real CR2s were:

* **Known pixel values.**  The frame is built at a chosen level, so the expected
  EV and clipping percentages can be asserted exactly instead of by inequality.
* **Chosen signals.**  Real daylight frames clip neither white nor black, which
  left those metrics untested.  :data:`CLIPPED_LEVEL` and :data:`CRUSHED_LEVEL`
  pin both ends.

The file extension is irrelevant: libraw identifies formats by content, so these
are written as ``.cr2`` wherever a test wants a realistic capture filename.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

# TIFF field types used below.
_BYTE, _ASCII, _SHORT, _LONG, _SRATIONAL = 1, 2, 3, 4, 10

#: 16-bit postprocess output saturates here, matching ``solramp.raw_analysis``.
WHITE_LEVEL = 65535

#: CFA levels chosen to land either side of the analyser's clipping thresholds
#: (``white_clip_frac=0.99`` -> 64880, ``black_clip_frac=0.01`` -> 655).
BRIGHT_LEVEL = 40000  # a well-exposed frame, ~-0.71 EV
DARK_LEVEL = 1200  # a late-dusk frame, ~-5.77 EV
CLIPPED_LEVEL = 65300  # every pixel above the white-clip threshold
CRUSHED_LEVEL = 200  # every pixel below the black-clip threshold

#: The crushed-black frame needs a narrower noise band than the default: at
#: level 200 the usual +/-200 spread puts part of the demosaiced distribution
#: back above the 655 black threshold, which only crushes ~84% of the pixels.
CRUSHED_SPREAD = 50

#: Default fixture size.  Must be even so the 2x2 Bayer pattern tiles exactly.
DEFAULT_SHAPE = (64, 96)

#: Canon 5D Mark IV sensor dimensions, for benchmarks that need a real workload.
FULL_FRAME_SHAPE = (4480, 6720)


def _pack(field_type: int, values) -> bytes:
    """Serialise a tag payload (ASCII values are NUL-terminated per TIFF)."""
    if field_type == _ASCII:
        return values.encode("ascii") + b"\x00"
    if field_type == _SRATIONAL:
        return b"".join(struct.pack("<ii", num, den) for num, den in values)
    fmt = {_BYTE: "<B", _SHORT: "<H", _LONG: "<I"}[field_type]
    return b"".join(struct.pack(fmt, v) for v in values)


def _value_count(field_type: int, values) -> int:
    return len(values) + 1 if field_type == _ASCII else len(values)


def build_dng(cfa: np.ndarray, *, white_level: int = WHITE_LEVEL) -> bytes:
    """Return the bytes of an uncompressed DNG wrapping a ``uint16`` CFA plane.

    ``cfa`` is a single-channel Bayer mosaic (not RGB): the tags below declare an
    RGGB pattern, so libraw demosaics it the way it would a real sensor readout.
    Colour handling is deliberately neutral -- an identity ``ColorMatrix1`` and
    no ``AsShotNeutral`` mean the white-balance multipliers come out as 1.0, so
    decoded RGB values match the levels written here.
    """
    if cfa.ndim != 2:
        raise ValueError(f"expected a 2-D CFA plane, got shape {cfa.shape!r}")
    if cfa.shape[0] % 2 or cfa.shape[1] % 2:
        raise ValueError(f"CFA dimensions must be even, got {cfa.shape!r}")

    height, width = cfa.shape
    raw_bytes = cfa.astype("<u2").tobytes()
    identity_matrix = [
        (1, 1), (0, 1), (0, 1),
        (0, 1), (1, 1), (0, 1),
        (0, 1), (0, 1), (1, 1),
    ]

    # (tag, type, values).  StripOffsets is patched once the layout is known.
    tags = [
        (254, _LONG, [0]),  # NewSubfileType: full-resolution image
        (256, _LONG, [width]),  # ImageWidth
        (257, _LONG, [height]),  # ImageLength
        (258, _SHORT, [16]),  # BitsPerSample
        (259, _SHORT, [1]),  # Compression: none
        (262, _SHORT, [32803]),  # PhotometricInterpretation: CFA
        (271, _ASCII, "Synthetic"),  # Make
        (272, _ASCII, "SolrampTestCam"),  # Model
        (273, _LONG, [0]),  # StripOffsets (patched below)
        (277, _SHORT, [1]),  # SamplesPerPixel
        (278, _LONG, [height]),  # RowsPerStrip: single strip
        (279, _LONG, [len(raw_bytes)]),  # StripByteCounts
        (284, _SHORT, [1]),  # PlanarConfiguration: chunky
        (33421, _SHORT, [2, 2]),  # CFARepeatPatternDim
        (33422, _BYTE, [0, 1, 1, 2]),  # CFAPattern: RGGB
        (50706, _BYTE, [1, 4, 0, 0]),  # DNGVersion 1.4
        (50707, _BYTE, [1, 1, 0, 0]),  # DNGBackwardVersion 1.1
        (50708, _ASCII, "SolrampTestCam"),  # UniqueCameraModel
        (50711, _SHORT, [1]),  # CFALayout: rectangular
        (50713, _SHORT, [1, 1]),  # BlackLevelRepeatDim
        (50714, _SHORT, [0]),  # BlackLevel
        (50717, _LONG, [white_level]),  # WhiteLevel
        (50721, _SRATIONAL, identity_matrix),  # ColorMatrix1
        (50778, _SHORT, [21]),  # CalibrationIlluminant1: D65
    ]
    tags.sort(key=lambda tag: tag[0])  # TIFF requires ascending tag order.

    ifd_offset = 8  # directly after the 8-byte TIFF header
    ifd_size = 2 + 12 * len(tags) + 4  # count + entries + next-IFD pointer
    heap_offset = ifd_offset + ifd_size

    # Payloads of <= 4 bytes live inline in the entry; longer ones go on a heap
    # after the IFD and are referenced by offset.
    heap = b""
    entries = []
    for tag, field_type, values in tags:
        payload = _pack(field_type, values)
        if len(payload) <= 4:
            inline = payload.ljust(4, b"\x00")
        else:
            inline = struct.pack("<I", heap_offset + len(heap))
            heap += payload
            if len(payload) % 2:
                heap += b"\x00"  # keep offsets word-aligned
        entries.append((tag, field_type, _value_count(field_type, values), inline))

    strip_offset = heap_offset + len(heap)
    entries = [
        (tag, ftype, count, struct.pack("<I", strip_offset) if tag == 273 else inline)
        for tag, ftype, count, inline in entries
    ]

    out = bytearray(b"II")  # little-endian byte order
    out += struct.pack("<HI", 42, ifd_offset)
    out += struct.pack("<H", len(entries))
    for tag, field_type, count, inline in entries:
        out += struct.pack("<HHI", tag, field_type, count) + inline
    out += struct.pack("<I", 0)  # no further IFDs
    out += heap
    out += raw_bytes
    return bytes(out)


def synth_cfa(
    level: int,
    *,
    shape: tuple[int, int] = DEFAULT_SHAPE,
    spread: int = 200,
    seed: int = 0,
) -> np.ndarray:
    """Return a ``uint16`` CFA plane centred on ``level``.

    ``spread`` adds a little seeded shot-noise so the frame is not perfectly
    flat (a degenerate input for percentile-based metrics) while keeping the
    mean at ``level``.  The fixed seed keeps generated files byte-identical
    between runs.
    """
    rng = np.random.default_rng(seed)
    noise = rng.integers(-spread, spread + 1, size=shape)
    return np.clip(level + noise, 0, WHITE_LEVEL).astype(np.uint16)


def write_synth_raw(
    path: Path,
    level: int,
    *,
    shape: tuple[int, int] = DEFAULT_SHAPE,
    spread: int = 200,
    seed: int = 0,
) -> Path:
    """Write a synthetic raw frame at ``level`` to ``path`` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_dng(synth_cfa(level, shape=shape, spread=spread, seed=seed)))
    return path


def expected_mean_ev(level: int) -> float:
    """The ``mean_ev`` a frame built at ``level`` should meter at.

    Luminance weights sum to 1.0, so a neutral frame's mean luminance is just
    ``level``; EV is its distance from saturation in stops.
    """
    return float(np.log2(level / WHITE_LEVEL))
