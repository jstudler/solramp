# SPDX-License-Identifier: AGPL-3.0-or-later
"""Timelapse metadata visualizer.

Reads ``timelapse_metadata.yaml`` side-car files written by the capture pipeline
and serves an interactive dashboard (Chart.js) that shows the session metadata, a
map of the capture location and synchronised diagrams of the per-frame exposure
metrics.

Each watch folder is searched recursively for ``timelapse_metadata.yaml``.  The
session identifier shown in the dropdown is the path of the directory holding the
YAML file, relative to its watch folder; the newest file (by modification time)
is selected by default.

The page polls the server for file changes, so a growing side-car (or a new
session folder appearing) refreshes the UI automatically.  Only the Python
standard library plus PyYAML is required; Chart.js is loaded from a CDN.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from .config import METADATA_FILENAME, Config
from .logging_setup import get_logger

_log = get_logger("visualizer")

try:
    # 6x faster than the pure-Python parser on a 756-frame sidecar (288ms -> 48ms)
    # and bundled with most PyYAML wheels.
    from yaml import CSafeLoader as _YamlLoader
except ImportError:  # PyYAML built without libyaml
    from yaml import SafeLoader as _YamlLoader  # type: ignore[assignment]

# A parsed sidecar is a few MB of dicts, so only the few in use are kept.
_PARSE_CACHE_SIZE = 4

# Capture time as the pipeline encodes it, e.g. "2026-06-16_02-55-16.cr2".
_FILENAME_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")
_UTC_OFFSET = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def _capture_time(frame: dict) -> str:
    """The shutter time taken from the filename, falling back to the analysis time.

    ``timestamp_analysis`` is when the RAW decode finished, so it drifts by however
    long the analysis took and makes the frame spacing look noisier than it was.
    """
    analysis = str(frame.get("timestamp_analysis", ""))
    match = _FILENAME_TIME.search(str(frame.get("filename", "")))
    if match is None:
        return analysis
    date, hour, minute, second = match.groups()
    # Filenames carry no zone, so reuse the one the capture host recorded.
    offset = _UTC_OFFSET.search(analysis)
    return f"{date}T{hour}:{minute}:{second}{offset.group(1) if offset else ''}"


# --------------------------------------------------------------------------- #
# Session discovery / loading
# --------------------------------------------------------------------------- #
class SessionRegistry:
    """Discovers and loads timelapse metadata files under one or more roots."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [r.resolve() for r in roots]
        self._multi = len(self.roots) > 1
        self._cache: dict[Path, tuple[tuple[float, int], tuple[dict, list]]] = {}

    def discover(self) -> dict[str, Path]:
        """Return a mapping of ``session id -> metadata file path``.

        Re-scans the filesystem on every call so newly added sessions appear
        without restarting the server.
        """
        found: dict[str, Path] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for yaml_path in sorted(root.rglob(METADATA_FILENAME)):
                ident = self._identifier(root, yaml_path.parent)
                # Disambiguate collisions across multiple roots.
                if ident in found:
                    ident = f"{root.name}/{ident}"
                found[ident] = yaml_path
        return found

    def _identifier(self, root: Path, session_dir: Path) -> str:
        try:
            rel = session_dir.resolve().relative_to(root)
        except ValueError:
            return session_dir.name
        rel_str = str(rel)
        if rel_str in (".", ""):
            base = root.name
        else:
            base = rel_str
        return f"{root.name}/{base}" if self._multi else base

    def list_sessions(self) -> list[dict]:
        """Lightweight session list with modification times, sorted newest first."""
        out: list[dict] = []
        for ident, path in self.discover().items():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            out.append({"id": ident, "mtime": mtime, "path": str(path)})
        out.sort(key=lambda item: item["mtime"], reverse=True)
        return out

    def load_session(self, ident: str) -> dict | None:
        """Load and normalise a single session's full data."""
        path = self.discover().get(ident)
        if path is None or not path.is_file():
            return None
        try:
            stat = path.stat()
        except OSError:
            return None

        parsed = self._parse(path, (stat.st_mtime, stat.st_size))
        if parsed is None:
            return None
        session, frames = parsed
        return {
            "id": ident,
            "path": str(path),
            "mtime": stat.st_mtime,
            "session": session,
            "frames": frames,
        }

    def _parse(self, path: Path, stamp: tuple[float, int]) -> tuple[dict, list] | None:
        """Parse ``path``, reusing the last result while it is untouched.

        The page polls, so the same unchanged sidecar is requested over and over,
        and parsing it costs far more than everything else the server does.
        """
        cached = self._cache.get(path)
        if cached is not None and cached[0] == stamp:
            self._cache[path] = self._cache.pop(path)  # keep it at the LRU tail
            return cached[1]

        started = time.perf_counter()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = yaml.load(handle, Loader=_YamlLoader) or {}  # nosec B506
        except (OSError, yaml.YAMLError):
            return None

        parsed = (
            data.get("session", {}) or {},
            [self._normalise_frame(f) for f in (data.get("frames") or [])],
        )
        _log.debug(
            "Parsed %s (%d frames) in %.0f ms",
            path, len(parsed[1]), (time.perf_counter() - started) * 1000,
        )
        self._cache[path] = (stamp, parsed)
        while len(self._cache) > _PARSE_CACHE_SIZE:
            self._cache.pop(next(iter(self._cache)))
        return parsed

    @staticmethod
    def _normalise_frame(frame: dict) -> dict:
        """Coerce string/number fields into a shape convenient for the client."""

        def num(value, default=None):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        return {
            "filename": frame.get("filename", ""),
            "t": _capture_time(frame),
            "analysis_duration_ms": num(frame.get("analysis_duration_ms")),
            "transfer_duration_ms": num(frame.get("transfer_duration_ms")),
            "iso": num(frame.get("iso")),
            "iso_label": str(frame.get("iso", "")),
            "aperture": num(frame.get("aperture")),
            "aperture_label": str(frame.get("aperture", "")),
            "shutter_label": str(frame.get("shutter", "")),
            "shutter_seconds": num(frame.get("shutter_seconds")),
            "clipping_white_pct": num(frame.get("clipping_white_pct"), 0.0),
            "clipping_black_pct": num(frame.get("clipping_black_pct"), 0.0),
            "mean_ev": num(frame.get("mean_ev")),
            "highlight_ev": num(frame.get("highlight_ev")),
            "mean_saturation": num(frame.get("mean_saturation")),
            "error_ev": num(frame.get("error_ev")),
            "phase": str(frame.get("phase", "")),
            "ramp_applied": str(frame.get("ramp_applied", "none")),
        }


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
def make_handler(registry: SessionRegistry):
    class Handler(BaseHTTPRequestHandler):
        # Silence the default noisy logging.
        def log_message(self, *_args) -> None:  # noqa: D401
            pass

        def _send_json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path

            if route == "/":
                self._send_html(INDEX_HTML)
                return

            if route == "/api/sessions":
                self._send_json({"sessions": registry.list_sessions()})
                return

            if route == "/api/session":
                params = parse_qs(parsed.query)
                ident = (params.get("id") or [""])[0]
                data = registry.load_session(ident)
                if data is None:
                    self._send_json({"error": "not found"}, status=404)
                else:
                    self._send_json(data)
                return

            self.send_error(404, "Not found")

    return Handler


# --------------------------------------------------------------------------- #
# Front-end (HTML + CSS + JS, served as a single page)
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Timelapse Metadata Visualizer</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/luxon@3.4.4/build/global/luxon.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.3.1/dist/chartjs-adapter-luxon.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1115;
    --panel: #181b22;
    --panel-2: #1f232c;
    --border: #2a2f3a;
    --text: #e6e8ec;
    --muted: #9aa3b2;
    --accent: #6ea8fe;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 14px 20px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 10;
    flex-wrap: wrap;
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .spacer { flex: 1; }
  select {
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 14px;
    min-width: 240px;
  }
  label.inline { color: var(--muted); font-size: 13px; }
  .status { color: var(--muted); font-size: 12px; }
  main { padding: 20px; max-width: 1280px; margin: 0 auto; }
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
    margin-bottom: 20px;
  }
  .panel h2 { font-size: 14px; margin: 0 0 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .session-grid { display: grid; grid-template-columns: 1fr 360px; gap: 18px; }
  @media (max-width: 880px) { .session-grid { grid-template-columns: 1fr; } }
  dl.meta { display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin: 0; font-size: 13px; }
  dl.meta dt { color: var(--muted); }
  dl.meta dd { margin: 0; font-variant-numeric: tabular-nums; }
  .map-wrap { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; height: 260px; background: var(--panel-2); }
  .map-wrap iframe { width: 100%; height: 100%; border: 0; }
  .map-link { display: block; font-size: 12px; margin-top: 6px; color: var(--accent); text-decoration: none; }
  .chart-box { position: relative; height: 320px; }
  .chart-box.tall { height: 360px; }
  .phase-legend { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .empty { color: var(--muted); font-size: 14px; padding: 40px; text-align: center; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px; background: var(--panel-2); border: 1px solid var(--border); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>🎞️ Timelapse Metadata Visualizer</h1>
  <label class="inline" for="sessionSelect">Session</label>
  <select id="sessionSelect"></select>
  <span class="spacer"></span>
  <span class="status" id="status">loading…</span>
</header>
<main>
  <div id="content"></div>
</main>

<script>
const PHASE_EMOJI = { darkening: "🌇", brightening: "🌅", day: "☀️", night: "🌙" };
let registry = [];          // [{id, mtime, path}]
let currentId = null;
let currentMtime = null;
let charts = [];            // active Chart instances (for synced hover)
let renderToken = 0;        // bumped per render, so stale async work can bail
let syncing = false;

// ---- helpers ------------------------------------------------------------- //
function fmtNum(v, digits = 3) {
  return (v === null || v === undefined || Number.isNaN(v)) ? "–" : Number(v).toFixed(digits);
}
function shutterTickLabel(seconds) {
  if (seconds >= 1) return seconds + "s";
  const inv = Math.round(1 / seconds);
  return "1/" + inv;
}
// Seconds elapsed since the previous frame; null where there is no predecessor.
function frameIntervals(frames) {
  const out = [];
  let prev = null;
  for (const f of frames) {
    const t = Date.parse(f.t);
    if (!Number.isFinite(t)) { out.push(null); continue; }
    out.push(prev === null ? null : (t - prev) / 1000);
    prev = t;
  }
  return out;
}

// ---- Chart.js plugins ---------------------------------------------------- //
// Vertical crosshair shared across all charts (synced hover).
const crosshairPlugin = {
  id: "crosshair",
  afterDraw(chart) {
    const idx = chart.$crosshairIndex;
    if (idx === null || idx === undefined) return;
    const meta = chart.getDatasetMeta(0);
    const el = meta && meta.data && meta.data[idx];
    if (!el) return;
    const { ctx, chartArea } = chart;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(el.x, chartArea.top);
    ctx.lineTo(el.x, chartArea.bottom);
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(110,168,254,0.65)";
    ctx.stroke();
    ctx.restore();
  },
};

// Vertical dashed lines where a ramp was applied (exposure chart only).
const rampPlugin = {
  id: "ramp",
  afterDatasetsDraw(chart) {
    const flags = chart.$rampFlags;
    if (!flags) return;
    const meta = chart.getDatasetMeta(0);
    if (!meta || !meta.data) return;
    const { ctx, chartArea } = chart;
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(255,193,79,0.55)";
    for (let i = 0; i < flags.length; i++) {
      if (!flags[i]) continue;
      const el = meta.data[i];
      if (!el) continue;
      ctx.beginPath();
      ctx.moveTo(el.x, chartArea.top);
      ctx.lineTo(el.x, chartArea.bottom);
      ctx.stroke();
    }
    ctx.restore();
  },
};

// Phase emoji strip drawn above the exposure chart plot area.
const phasePlugin = {
  id: "phase",
  afterDatasetsDraw(chart) {
    const phases = chart.$phases;
    if (!phases || !phases.length) return;
    const meta = chart.getDatasetMeta(0);
    if (!meta || !meta.data) return;
    const { ctx, chartArea } = chart;
    ctx.save();
    ctx.font = "16px system-ui";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const y = chartArea.top - 12;
    // One emoji per contiguous phase segment, centred over the segment.
    let segStart = 0;
    for (let i = 1; i <= phases.length; i++) {
      if (i === phases.length || phases[i] !== phases[segStart]) {
        const a = meta.data[segStart], b = meta.data[i - 1];
        if (a && b) {
          const emoji = PHASE_EMOJI[phases[segStart]] || "❓";
          ctx.fillText(emoji, (a.x + b.x) / 2, y);
        }
        segStart = i;
      }
    }
    ctx.restore();
  },
};
Chart.register(crosshairPlugin, rampPlugin, phasePlugin);

// ---- synced hover -------------------------------------------------------- //
function syncIndex(idx) {
  if (syncing) return;
  syncing = true;
  for (const ch of charts) {
    ch.$crosshairIndex = idx;
    if (idx === null) {
      ch.setActiveElements([]);
      ch.tooltip.setActiveElements([], { x: 0, y: 0 });
    } else {
      const els = ch.data.datasets.map((d, i) => ({ datasetIndex: i, index: idx }));
      ch.setActiveElements(els);
      ch.tooltip.setActiveElements(els, { x: 0, y: 0 });
    }
    ch.update();
  }
  syncing = false;
}

function hoverHandler(evt, _active, chart) {
  const pts = chart.getElementsAtEventForMode(evt, "index", { intersect: false }, false);
  syncIndex(pts.length ? pts[0].index : null);
}

// ---- chart construction -------------------------------------------------- //
function timeScale(title) {
  return {
    type: "time",
    time: { tooltipFormat: "yyyy-LL-dd HH:mm:ss" },
    title: { display: true, text: title },
    ticks: { maxRotation: 0, autoSkip: true, color: "#9aa3b2" },
    grid: { color: "rgba(255,255,255,0.05)" },
  };
}

function baseOptions(frames) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    onHover: hoverHandler,
    plugins: {
      legend: { labels: { color: "#e6e8ec", boxWidth: 12 } },
      tooltip: {
        callbacks: {
          title(items) {
            if (!items.length) return "";
            const f = frames[items[0].dataIndex];
            return f ? f.t : "";
          },
          afterTitle(items) {
            if (!items.length) return "";
            const f = frames[items[0].dataIndex];
            return f ? "📄 " + f.filename : "";
          },
        },
      },
    },
  };
}

function buildExposureChart(ctx, frames) {
  const labels = frames.map(f => f.t);
  const phases = frames.map(f => f.phase);
  const rampFlags = frames.map(f => f.ramp_applied && f.ramp_applied !== "none");
  const intervals = frameIntervals(frames);

  const opts = baseOptions(frames);
  opts.layout = { padding: { top: 26 } };
  opts.scales = {
    x: timeScale("Time"),
    yShutter: {
      type: "logarithmic",
      position: "left",
      title: { display: true, text: "Shutter (s)" },
      ticks: { color: "#9aa3b2", callback: (v) => shutterTickLabel(v) },
      grid: { color: "rgba(255,255,255,0.05)" },
    },
    yIso: {
      type: "linear",
      position: "right",
      title: { display: true, text: "ISO" },
      ticks: { color: "#9aa3b2" },
      grid: { drawOnChartArea: false },
    },
    yAperture: {
      type: "linear",
      position: "right",
      title: { display: true, text: "Aperture (f/)" },
      ticks: { color: "#9aa3b2", callback: (v) => "f/" + v },
      grid: { drawOnChartArea: false },
      offset: true,
    },
  };
  opts.plugins.tooltip.callbacks.label = (item) => {
    const f = frames[item.dataIndex];
    switch (item.dataset.label) {
      case "Shutter (s)": return "Shutter: " + f.shutter_label + "  (" + fmtNum(f.shutter_seconds, 5) + "s)";
      case "ISO": return "ISO: " + (f.iso_label || fmtNum(f.iso, 0));
      case "Aperture": return "Aperture: f/" + (f.aperture_label || fmtNum(f.aperture, 1));
      case "Interval": {
        const v = intervals[item.dataIndex];
        return v === null ? "Interval: –" : "Interval: " + fmtNum(v, 1) + " s";
      }
      default: return item.dataset.label + ": " + item.formattedValue;
    }
  };

  const chart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Shutter (s)", yAxisID: "yShutter",
          data: frames.map(f => f.shutter_seconds),
          borderColor: "#6ea8fe", backgroundColor: "#6ea8fe",
          stepped: true, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2,
        },
        {
          label: "ISO", yAxisID: "yIso",
          data: frames.map(f => f.iso),
          borderColor: "#f78fb3", backgroundColor: "#f78fb3",
          stepped: true, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2,
        },
        {
          label: "Aperture", yAxisID: "yAperture",
          data: frames.map(f => f.aperture),
          borderColor: "#7bed9f", backgroundColor: "#7bed9f",
          stepped: true, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2,
        },
        {
          label: "Interval", yAxisID: "yShutter",
          data: intervals,
          borderColor: "#b78bff", backgroundColor: "#b78bff",
          pointRadius: 0, pointHoverRadius: 4, borderWidth: 1.5,
        },
      ],
    },
    options: opts,
  });
  chart.$phases = phases;
  chart.$rampFlags = rampFlags;
  chart.$crosshairIndex = null;
  return chart;
}

function buildAnalysisChart(ctx, frames) {
  const labels = frames.map(f => f.t);
  const opts = baseOptions(frames);
  opts.scales = {
    x: timeScale("Time"),
    yPct: {
      type: "linear", position: "left", min: 0, max: 100,
      title: { display: true, text: "Clipping (%)" },
      ticks: { color: "#9aa3b2", callback: (v) => v + "%" },
      grid: { color: "rgba(255,255,255,0.05)" },
    },
    yEV: {
      type: "linear", position: "right",
      title: { display: true, text: "EV" },
      ticks: { color: "#9aa3b2" },
      grid: { drawOnChartArea: false },
    },
    ySat: {
      type: "linear", position: "right", min: 0, max: 1,
      title: { display: true, text: "Saturation" },
      ticks: { color: "#9aa3b2" },
      grid: { drawOnChartArea: false },
      offset: true,
    },
  };
  opts.plugins.tooltip.callbacks.label = (item) => {
    const f = frames[item.dataIndex];
    switch (item.dataset.label) {
      case "Clipping white": return "Clipping white: " + fmtNum(f.clipping_white_pct, 2) + "%";
      case "Clipping black": return "Clipping black: " + fmtNum(f.clipping_black_pct, 2) + "%";
      case "Mean EV": return "Mean EV: " + fmtNum(f.mean_ev, 3);
      case "Highlight EV": return "Highlight EV: " + fmtNum(f.highlight_ev, 3);
      case "Error EV": return "Error EV: " + fmtNum(f.error_ev, 3);
      case "Mean saturation": return "Mean saturation: " + fmtNum(f.mean_saturation, 3);
      default: return item.dataset.label + ": " + item.formattedValue;
    }
  };

  const chart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          // White clipping fills from the TOP of the scale downwards.
          label: "Clipping white", yAxisID: "yPct",
          data: frames.map(f => 100 - f.clipping_white_pct),
          borderColor: "rgba(255,255,255,0.55)",
          backgroundColor: "rgba(255,255,255,0.16)",
          fill: "end", pointRadius: 0, pointHoverRadius: 4, borderWidth: 1,
        },
        {
          // Black clipping fills from the BOTTOM (origin) upwards.
          label: "Clipping black", yAxisID: "yPct",
          data: frames.map(f => f.clipping_black_pct),
          borderColor: "rgba(120,130,150,0.7)",
          backgroundColor: "rgba(40,46,60,0.55)",
          fill: "origin", pointRadius: 0, pointHoverRadius: 4, borderWidth: 1,
        },
        {
          label: "Mean EV", yAxisID: "yEV",
          data: frames.map(f => f.mean_ev),
          borderColor: "#6ea8fe", backgroundColor: "#6ea8fe",
          pointRadius: 0, pointHoverRadius: 4, borderWidth: 2,
        },
        {
          label: "Highlight EV", yAxisID: "yEV",
          data: frames.map(f => f.highlight_ev),
          borderColor: "#ffd479", backgroundColor: "#ffd479",
          pointRadius: 0, pointHoverRadius: 4, borderWidth: 2,
        },
        {
          label: "Error EV", yAxisID: "yEV",
          data: frames.map(f => f.error_ev),
          borderColor: "#ff7b72", backgroundColor: "#ff7b72",
          borderDash: [5, 3], pointRadius: 0, pointHoverRadius: 4, borderWidth: 2,
        },
        {
          label: "Mean saturation", yAxisID: "ySat",
          data: frames.map(f => f.mean_saturation),
          borderColor: "#7bed9f", backgroundColor: "#7bed9f",
          pointRadius: 0, pointHoverRadius: 4, borderWidth: 2,
        },
      ],
    },
    options: opts,
  });
  chart.$crosshairIndex = null;
  return chart;
}

// ---- rendering ----------------------------------------------------------- //
function destroyCharts() {
  for (const ch of charts) ch.destroy();
  charts = [];
}

function renderSession(data) {
  destroyCharts();
  const token = ++renderToken;
  const content = document.getElementById("content");
  const s = data.session || {};
  const loc = s.location || {};
  const init = s.initial_settings || {};
  const frames = data.frames || [];

  const hasCoords = typeof loc.latitude === "number" && typeof loc.longitude === "number";
  let mapHtml = '<div class="empty">No coordinates in metadata.</div>';
  if (hasCoords) {
    const lat = loc.latitude, lon = loc.longitude;
    const d = 0.02;
    const bbox = [lon - d, lat - d, lon + d, lat + d].join("%2C");
    const src = "https://www.openstreetmap.org/export/embed.html?bbox=" + bbox +
      "&layer=mapnik&marker=" + lat + "%2C" + lon;
    const link = "https://www.openstreetmap.org/?mlat=" + lat + "&mlon=" + lon + "#map=13/" + lat + "/" + lon;
    mapHtml =
      '<div class="map-wrap"><iframe loading="lazy" src="' + src + '"></iframe></div>' +
      '<a class="map-link" href="' + link + '" target="_blank" rel="noopener">View larger map ↗</a>';
  }

  function row(label, value) {
    if (value === undefined || value === null || value === "") return "";
    return "<dt>" + label + "</dt><dd>" + value + "</dd>";
  }
  const runCount = Array.isArray(s.runs) ? s.runs.length : 0;
  const metaRows =
    row("Session", '<span class="pill">' + data.id + "</span>") +
    row("Started", s.started_at) +
    row("App version", s.app_version) +
    row("Interval", s.interval_seconds !== undefined ? s.interval_seconds + " s" : "") +
    row("Camera", s.camera_address) +
    row("Auto-init", s.auto_init === undefined ? "" : (s.auto_init ? "yes" : "no")) +
    row("Location", loc.name) +
    row("Coordinates", hasCoords ? (loc.latitude + ", " + loc.longitude) : "") +
    row("Timezone", loc.timezone) +
    row("Initial ISO / Aperture / Shutter",
        (init.iso || init.aperture || init.shutter)
          ? ("ISO " + (init.iso || "?") + " · f/" + (init.aperture || "?") + " · " + (init.shutter || "?"))
          : "") +
    row("Runs", runCount || "") +
    row("Frames", frames.length);

  content.innerHTML =
    '<div class="panel">' +
      '<h2>Session</h2>' +
      '<div class="session-grid">' +
        '<dl class="meta">' + metaRows + '</dl>' +
        '<div>' + mapHtml + '</div>' +
      '</div>' +
    '</div>' +
    '<div class="panel">' +
      '<h2>Exposure (ISO · Aperture · Shutter · Interval)</h2>' +
      '<div class="phase-legend">Phase: 🌇 darkening (sunset) · 🌅 brightening (sunrise) · dashed amber line = ramp applied</div>' +
      '<div class="chart-box tall"><canvas id="exposureChart"></canvas></div>' +
    '</div>' +
    '<div class="panel">' +
      '<h2>Analysis (Clipping · EV · Saturation)</h2>' +
      '<div class="chart-box tall"><canvas id="analysisChart"></canvas></div>' +
    '</div>';

  if (!frames.length) {
    content.querySelectorAll(".chart-box").forEach(b => {
      b.innerHTML = '<div class="empty">No frames recorded yet.</div>';
    });
    return;
  }

  buildChartsSoon(frames, token);
}

// Build the charts in a later task so the session panel and map paint first;
// the token drops the work if another render has replaced the canvases since.
function buildChartsSoon(frames, token) {
  requestAnimationFrame(() => setTimeout(() => {
    if (token !== renderToken) return;
    charts = [
      buildExposureChart(document.getElementById("exposureChart").getContext("2d"), frames),
      buildAnalysisChart(document.getElementById("analysisChart").getContext("2d"), frames),
    ];
    for (const ch of charts) {
      ch.canvas.addEventListener("mouseleave", () => syncIndex(null));
    }
  }, 0));
}

// ---- data loading / polling --------------------------------------------- //
async function fetchJSON(url) {
  const resp = await fetch(url, { cache: "no-store" });
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return resp.json();
}

function populateDropdown() {
  const sel = document.getElementById("sessionSelect");
  const prev = sel.value;
  sel.innerHTML = "";
  for (const s of registry) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.id;
    sel.appendChild(opt);
  }
  if (registry.some(s => s.id === prev)) {
    sel.value = prev;
  } else if (registry.length) {
    sel.value = registry[0].id;  // newest (list is sorted newest first)
  }
}

async function loadSelected(force = false) {
  const id = document.getElementById("sessionSelect").value;
  if (!id) {
    document.getElementById("content").innerHTML =
      '<div class="panel"><div class="empty">No timelapse_metadata.yaml files found.</div></div>';
    return;
  }
  const entry = registry.find(s => s.id === id);
  const mtime = entry ? entry.mtime : null;
  if (!force && id === currentId && mtime === currentMtime) return;

  const data = await fetchJSON("/api/session?id=" + encodeURIComponent(id));
  if (data && !data.error) {
    currentId = id;
    currentMtime = data.mtime;
    renderSession(data);
    setStatus("updated " + new Date().toLocaleTimeString());
  }
}

function setStatus(text) {
  document.getElementById("status").textContent = text;
}

async function poll() {
  try {
    const data = await fetchJSON("/api/sessions");
    registry = data.sessions || [];
    populateDropdown();
    await loadSelected();
  } catch (err) {
    setStatus("connection lost…");
  }
}

document.getElementById("sessionSelect").addEventListener("change", () => loadSelected(true));

(async function init() {
  await poll();
  setInterval(poll, 2000);
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #
def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in {"localhost", ""}


class VisualizerServer:
    """The dashboard as a start/stop background service.

    Runs on a daemon thread so it can live next to the capture pipeline, and
    never raises out of :meth:`start` - a dashboard that cannot bind must not
    take a timelapse down with it.
    """

    def __init__(
        self,
        roots: list[Path],
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        open_browser: bool = False,
    ) -> None:
        self.registry = SessionRegistry(roots)
        self.host = host
        self.requested_port = port
        self.open_browser = open_browser
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        """The port actually bound (differs from the request when asked for 0)."""
        return self._server.server_address[1] if self._server else None

    @property
    def url(self) -> str | None:
        return f"http://{self.host}:{self.port}/" if self._server else None

    def start(self) -> bool:
        """Bind and serve. Returns False (and logs) if the port is unusable."""
        if self._server is not None:
            return True
        try:
            server = ThreadingHTTPServer(
                (self.host, self.requested_port), make_handler(self.registry)
            )
        except OSError as exc:
            _log.warning(
                "Visualizer disabled: cannot bind %s:%d (%s)",
                self.host, self.requested_port, exc,
            )
            return False
        # Otherwise shutdown() would block on clients mid-poll.
        server.daemon_threads = True
        self._server = server

        if not _is_loopback(self.host):
            _log.warning(
                "Visualizer is bound to %s and has no authentication; anyone who "
                "can reach this port sees your capture paths and GPS coordinates",
                self.host,
            )
        self._thread = threading.Thread(
            target=server.serve_forever, name="visualizer", daemon=True
        )
        self._thread.start()
        _log.info(
            "Visualizer serving %s (watching %s; %d session(s) found)",
            self.url,
            ", ".join(str(r) for r in self.registry.roots),
            len(self.registry.list_sessions()),
        )
        if self.open_browser:
            url = self.url
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        return True

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None
        _log.info("Visualizer stopped")


def start_visualizer(config: Config) -> VisualizerServer | None:
    """Start the dashboard for ``config``, or return ``None`` if it cannot run."""
    server = VisualizerServer(
        config.visualizer_watch_folders,
        host=config.visualizer_host,
        port=config.visualizer_port,
        open_browser=config.visualizer_open_browser,
    )
    return server if server.start() else None

