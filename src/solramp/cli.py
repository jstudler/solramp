# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-documenting command line interface.

Every option may also live in the TOML config file (``--config``); a value given
on the command line always wins.  The capture output directory is the one
setting that is CLI-only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .app import run, run_visualizer_only
from .config import SYNC_BACKLOG_POLICIES, ConfigError, build_config
from .daemon import supervise
from .logging_setup import configure_logging, get_logger

_log = get_logger("cli")

# Advanced ramping options exposed on the CLI. Each maps to a TOML key in the
# [ramping] table and to the cli key "ramp_<key>" expected by build_config.
_RAMP_FLOAT_OPTIONS = [
    ("target-headroom-stops", "keep highlights this many stops below clipping"),
    ("deadband-stops", "ignore exposure errors smaller than this (stops)"),
    ("white-clip-pct-threshold", "%% white-clipped pixels that forces darkening"),
    ("black-clip-pct-threshold", "%% black-clipped pixels that allows brightening"),
    ("target-headroom-stops-night", "highlight headroom at night (blended by sun altitude)"),
    ("white-clip-pct-threshold-night", "%% white-clipped pixels tolerated at night"),
    ("black-clip-pct-limit", "hard %% black-clip ceiling that forces brightening (day+night)"),
    ("day-altitude-deg", "sun altitude at/above which full day tolerances apply"),
    ("night-altitude-deg", "sun altitude at/below which full night tolerances apply"),
    ("dawn-proactive-black-pct", "%% black-clip threshold for proactive dawn darkening"),
    ("highlight-percentile", "luminance percentile used as the ETTR indicator"),
    ("white-clip-frac", "channel fraction of full-scale counted as white clip"),
    ("black-clip-frac", "channel fraction of full-scale counted as black clip"),
    ("trend-ev-threshold", "per-frame EV error magnitude counted as a fast trend"),
]
_RAMP_LABEL_OPTIONS = [
    ("iso-dark-limit", "lowest ISO the ramp will use"),
    ("iso-bright-limit", "highest ISO the ramp will use"),
    ("aperture-dark-limit", "narrowest aperture (largest f-number) the ramp will use"),
    ("aperture-bright-limit", "widest aperture (smallest f-number) the ramp will use"),
    ("shutter-dark-limit", "fastest shutter the ramp will use"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solramp",
        description=(
            "Sun-driven day-to-night timelapse controller for the Canon EOS 5D "
            "Mark IV. Captures at a fixed interval and ramps ISO/aperture/shutter "
            "to hold an ETTR exposure (LRTimelapse-compatible)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Defaults are intentionally None so that absent CLI args fall through to the
    # TOML file / built-in defaults inside build_config().
    parser.add_argument(
        "--config", type=Path, default=None, help="path to a TOML config file"
    )
    parser.add_argument(
        "--interval", dest="interval_seconds", type=float, default=None,
        help="seconds between captures (REQUIRED, minimum 20)",
    )
    parser.add_argument(
        "--output-dir", dest="output_dir", type=str, default=None,
        help="capture directory (CLI-only); default ./%%Y-%%m-%%d-%%H-%%M-%%S",
    )
    parser.add_argument(
        "--camera-transport", dest="camera_transport", type=str, default=None,
        choices=["ptpip", "usb"],
        help="camera connection transport",
    )
    parser.add_argument(
        "--camera-address", dest="camera_address", type=str, default=None,
        help="camera IP or mDNS hostname (required for --camera-transport ptpip)",
    )
    parser.add_argument(
        "--camera-selector", dest="camera_selector", type=str, default=None,
        help="USB camera selector (port/model/serial fragment) when using --camera-transport usb",
    )
    parser.add_argument(
        "--auto-init", dest="auto_init", action="store_true", default=None,
        help="auto-find a safe ETTR starting exposure",
    )
    parser.add_argument(
        "--auto-init-keep-files", dest="auto_init_keep_files",
        action=argparse.BooleanOptionalAction, default=None,
        help="keep auto-init test frames in .autoinit/ (default: keep)",
    )
    parser.add_argument(
        "--create-timestamped-subdirectory", dest="create_timestamped_subdirectory",
        action=argparse.BooleanOptionalAction, default=None,
        help="work inside a timestamped subfolder of --output-dir (default: yes)",
    )
    parser.add_argument(
        "--long-exposure-nr", dest="long_exposure_nr",
        action=argparse.BooleanOptionalAction, default=None,
        help="leave in-camera Long Exposure NR enabled (default: yes; "
             "disable to halve long-exposure cycle time)",
    )

    daemon = parser.add_argument_group("daemon mode")
    daemon.add_argument(
        "--daemon", dest="daemon", action="store_true", default=None,
        help="keep running: wait for the camera, shoot a session per "
             "connection, and start a new folder after each outage "
             "(requires --output-dir)",
    )
    daemon.add_argument(
        "--camera-lost-cycles", dest="camera_lost_cycles", type=float, default=None,
        help="end the session after this many missed intervals without a frame",
    )
    daemon.add_argument(
        "--daemon-poll-seconds", dest="daemon_poll_seconds", type=float, default=None,
        help="how often to look for the camera while idle",
    )

    sync = parser.add_argument_group(
        "remote sync (mandatory when --output-dir is RAM-backed, e.g. /dev/shm)"
    )
    sync.add_argument(
        "--rsync-dest", dest="rsync_dest", type=str, default=None,
        help="rsync destination base: user@host:/path, an ~/.ssh/config alias, "
             "or a local directory; each session gets its own sub-folder",
    )
    sync.add_argument(
        "--rsync-ssh-key", dest="rsync_ssh_key", type=Path, default=None,
        help="ssh private key file (default: whatever ssh resolves from ~/.ssh)",
    )
    sync.add_argument(
        "--rsync-ssh-port", dest="rsync_ssh_port", type=int, default=None,
        help="ssh port (default: whatever ssh resolves from ~/.ssh/config)",
    )
    sync.add_argument(
        "--rsync-bwlimit", dest="rsync_bwlimit", type=int, default=None,
        help="cap transfer rate in KiB/s (useful on metered 5G)",
    )
    sync.add_argument(
        "--rsync-timeout", dest="rsync_timeout", type=float, default=None,
        help="per-transfer I/O timeout in seconds",
    )
    sync.add_argument(
        "--delete-synced-raw", dest="delete_synced_raw",
        action=argparse.BooleanOptionalAction, default=None,
        help="delete each CR2 once the receiver confirms it "
             "(default: yes on RAM storage, no otherwise)",
    )
    sync.add_argument(
        "--sync-backlog-limit", dest="sync_backlog_limit", type=int, default=None,
        help="un-synced frames tolerated before --on-sync-backlog kicks in",
    )
    sync.add_argument(
        "--on-sync-backlog", dest="on_sync_backlog", default=None,
        choices=list(SYNC_BACKLOG_POLICIES),
        help="what to do when uploads cannot keep up: stop the session, or "
             "drop the oldest un-synced frames to free RAM",
    )

    visualizer = parser.add_argument_group("visualizer dashboard")
    visualizer.add_argument(
        "--visualizer", dest="visualizer", action=argparse.BooleanOptionalAction,
        default=None,
        help="serve the metadata dashboard alongside the capture (default: yes)",
    )
    visualizer.add_argument(
        "--visualizer-only", dest="visualizer_only", action="store_true", default=None,
        help="serve the dashboard and nothing else: no camera, no capture",
    )
    visualizer.add_argument(
        "--visualizer-host", dest="visualizer_host", default=None,
        help="dashboard bind address; anything other than 127.0.0.1 exposes it "
             "un-authenticated to the network",
    )
    visualizer.add_argument(
        "--visualizer-port", dest="visualizer_port", type=int, default=None,
        help="dashboard port (default 8000; 0 picks any free port)",
    )
    visualizer.add_argument(
        "--visualizer-open-browser", dest="visualizer_open_browser",
        action=argparse.BooleanOptionalAction, default=None,
        help="open a browser on start (default: only with --visualizer-only)",
    )
    visualizer.add_argument(
        "--additional-visualizer-folders", dest="additional_visualizer_folders",
        nargs="+", action="extend", type=Path, default=None, metavar="DIR",
        help="extra folders to scan for timelapse_metadata.yaml, on top of "
             "--output-dir",
    )

    parser.add_argument(
        "--log-level", dest="log_level_stdout", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="stdout logging verbosity",
    )
    parser.add_argument(
        "--log-file", dest="log_file", action=argparse.BooleanOptionalAction,
        default=None,
        help="write a per-session log file into the capture directory",
    )
    parser.add_argument(
        "--log-level-file", dest="log_level_file", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log file verbosity (default DEBUG)",
    )
    parser.add_argument(
        "--log-rotate", dest="log_rotate", default=None, metavar="DURATION",
        help="start a new log file every DURATION, aligned to the wall clock "
             "(e.g. 10m, 1h, 4h, 1d; 'off' for a single file; default 1h). "
             "With --rsync-dest, finished files are deleted locally once shipped",
    )

    location = parser.add_argument_group("location (for sun-altitude phase)")
    location.add_argument("--latitude", type=float, default=None)
    location.add_argument("--longitude", type=float, default=None)
    location.add_argument("--timezone", type=str, default=None, help="IANA tz name")
    location.add_argument("--elevation", type=float, default=None, help="metres ASL")
    location.add_argument("--location-name", dest="location_name", default=None)

    timing = parser.add_argument_group("timing")
    timing.add_argument("--min-interval", dest="min_interval_seconds", type=float, default=None)
    timing.add_argument("--shutter-margin", dest="shutter_margin_seconds", type=float, default=None)
    timing.add_argument("--capture-retries", dest="capture_retries", type=int, default=None)
    timing.add_argument("--download-retries", dest="download_retries", type=int, default=None)

    ramp = parser.add_argument_group("ramping / exposure tuning")
    for flag, help_text in _RAMP_FLOAT_OPTIONS:
        ramp.add_argument(
            f"--{flag}", dest=f"ramp_{flag.replace('-', '_')}", type=float,
            default=None, help=help_text,
        )
    ramp.add_argument(
        "--half-size-decode", dest="ramp_half_size_decode",
        action=argparse.BooleanOptionalAction, default=None,
        help="decode CR2 at half resolution for faster analysis",
    )
    ramp.add_argument(
        "--trend-window", dest="ramp_trend_window", type=int, default=None,
        help="consecutive frames to confirm a fast trend",
    )
    ramp.add_argument(
        "--dawn-proactive-mean-ev-window", dest="ramp_dawn_proactive_mean_ev_window",
        type=int, default=None,
        help="frames of mean_ev history for proactive dawn rising detection",
    )
    for flag, help_text in _RAMP_LABEL_OPTIONS:
        ramp.add_argument(
            f"--{flag}", dest=f"ramp_{flag.replace('-', '_')}", type=str,
            default=None, help=help_text,
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure logging early so config errors are visible and formatted.
    configure_logging(args.log_level_stdout or "INFO")

    cli_overrides = vars(args).copy()
    toml_path = cli_overrides.pop("config", None)
    try:
        config = build_config(cli_overrides, toml_path=toml_path)
    except ConfigError as exc:
        _log.error("Configuration error: %s", exc)
        return 2

    if config.visualizer_only:
        # No capture folder exists, so logging stays on stdout.
        configure_logging(config.log_level_stdout)
        return run_visualizer_only(config)

    if config.daemon:
        # Logging stays on stdout here; the supervisor points it at a session
        # log file only once a session actually starts, so no empty dated
        # folder is created while waiting for the camera.
        configure_logging(config.log_level_stdout)
        return supervise(cli_overrides, toml_path)

    # Re-apply the (possibly TOML-sourced) levels now that config is resolved,
    # adding the per-session log file alongside the captures.
    configure_logging(
        config.log_level_stdout,
        log_file=config.log_file_path,
        file_level=config.log_level_file,
        rotate_seconds=config.log_rotate_seconds,
    )
    try:
        return run(config)
    except KeyboardInterrupt:
        # Defensive: signal handler normally turns this into a graceful stop.
        _log.info("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
