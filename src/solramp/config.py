# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration: merge TOML file + CLI overrides, validate, and build the
strongly-typed objects the rest of the app consumes.

Precedence (highest wins): CLI argument > TOML value > built-in default.

The capture *output directory* is intentionally CLI-only (never read from TOML)
as required by the spec; everything else may live in either place.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .astral_phase import LocationConfig
from .ramping import RampParams
from .sync import RsyncDest, SyncError, validate_ssh_key


class ConfigError(Exception):
    """Raised for missing required parameters or invalid configuration."""


# Hard floor on the interval: the camera needs ~15 s to ship a ~33 MB CR2.
DEFAULT_MIN_INTERVAL_SECONDS = 20.0
# Shutter must finish with time to spare before the next capture.
DEFAULT_SHUTTER_MARGIN_SECONDS = 5.0
# The camera cannot expose longer than this regardless of interval.
CAMERA_MAX_SHUTTER_SECONDS = 30.0
DEFAULT_CAMERA_TRANSPORT = "ptpip"
DEFAULT_CAMERA_ADDRESS = "192.168.0.104"
METADATA_FILENAME = "timelapse_metadata.yaml"
LOG_FILENAME = "solramp.log"
DEFAULT_LOG_ROTATE = "1h"
MIN_LOG_ROTATE_SECONDS = 60
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_PATTERN = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_DISABLED_WORDS = frozenset({"off", "none", "no", "false", "0"})
DEFAULT_RSYNC_TIMEOUT_SECONDS = 120.0
DEFAULT_SYNC_BACKLOG_LIMIT = 20
SYNC_BACKLOG_POLICIES = ("abort", "drop-oldest")
# The dashboard has no authentication, so it stays on loopback unless asked.
DEFAULT_VISUALIZER_HOST = "127.0.0.1"
DEFAULT_VISUALIZER_PORT = 8000

# Paths that are RAM-backed on every mainstream Linux distribution. Checked by
# name as well as by fstype so detection still works off-Linux (e.g. in tests).
_MEMORY_PATH_PREFIXES = ("/dev/shm", "/run/shm")  # nosec B108
_MEMORY_FSTYPES = frozenset({"tmpfs", "ramfs"})
_MOUNT_ESCAPES = (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\"))


def _unescape_mount_point(mount_field: str) -> str:
    for escape, literal in _MOUNT_ESCAPES:
        mount_field = mount_field.replace(escape, literal)
    return mount_field


def _read_proc_mounts() -> str | None:
    try:
        return Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError:
        return None


def memory_mount_point(
    path: Path, *, mounts_text: str | None = None
) -> Path | None:
    """Return the tmpfs/ramfs mount point ``path`` lives on, else ``None``.

    ``mounts_text`` overrides ``/proc/mounts`` so the logic is testable on a
    machine that has no procfs.
    """
    resolved = Path(path).expanduser().resolve()
    parents = set(resolved.parents)
    for prefix in _MEMORY_PATH_PREFIXES:
        candidate = Path(prefix)
        if resolved == candidate or candidate in parents:
            return candidate

    if mounts_text is None:
        mounts_text = _read_proc_mounts()
    if mounts_text is None:
        return None

    # Longest matching mount wins across *all* filesystems, so a non-tmpfs
    # mounted inside a tmpfs is correctly reported as durable storage.
    best: Path | None = None
    best_fstype = ""
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point = Path(_unescape_mount_point(fields[1]))
        if resolved != mount_point and mount_point not in parents:
            continue
        if best is None or len(mount_point.parts) >= len(best.parts):
            best, best_fstype = mount_point, fields[2]
    return best if best_fstype in _MEMORY_FSTYPES else None


def is_memory_backed(path: Path, *, mounts_text: str | None = None) -> bool:
    """True when ``path`` lives in RAM and so cannot retain CR2 files."""
    return memory_mount_point(path, mounts_text=mounts_text) is not None


# Daemon defaults: end a session after this many missed intervals, and look for
# the camera this often while idle.
DEFAULT_CAMERA_LOST_CYCLES = 5.0
DEFAULT_DAEMON_POLL_SECONDS = 15.0


@dataclass
class Config:
    """Fully-resolved, validated application configuration."""

    camera_transport: str
    camera_address: str | None
    camera_selector: str | None
    interval_seconds: float
    output_dir: Path
    location: LocationConfig
    ramp: RampParams
    auto_init: bool = False
    auto_init_keep_files: bool = True
    create_timestamped_subdirectory: bool = True
    long_exposure_nr: bool = True
    shutter_margin_seconds: float = DEFAULT_SHUTTER_MARGIN_SECONDS
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    log_level_stdout: str = "INFO"
    log_file: bool = True
    log_level_file: str = "DEBUG"
    # Seconds per log file bucket; None writes one un-rotated log file.
    log_rotate_seconds: int | None = 3600
    metadata_filename: str = METADATA_FILENAME
    log_filename: str = LOG_FILENAME
    capture_retries: int = 5
    download_retries: int = 3
    daemon: bool = False
    camera_lost_cycles: float = DEFAULT_CAMERA_LOST_CYCLES
    daemon_poll_seconds: float = DEFAULT_DAEMON_POLL_SECONDS
    rsync_dest: str | None = None
    rsync_ssh_key: Path | None = None
    rsync_ssh_port: int | None = None
    rsync_bwlimit: int | None = None
    rsync_timeout: float = DEFAULT_RSYNC_TIMEOUT_SECONDS
    delete_synced_raw: bool = False
    sync_backlog_limit: int = DEFAULT_SYNC_BACKLOG_LIMIT
    on_sync_backlog: str = "abort"
    output_dir_in_memory: bool = False
    # --output-dir as given, i.e. the parent of the timestamped session folders.
    output_base_dir: Path = Path(".")
    visualizer: bool = True
    visualizer_only: bool = False
    visualizer_host: str = DEFAULT_VISUALIZER_HOST
    visualizer_port: int = DEFAULT_VISUALIZER_PORT
    visualizer_open_browser: bool = False
    visualizer_additional_folders: tuple[Path, ...] = ()

    @property
    def visualizer_watch_folders(self) -> list[Path]:
        """Folders the dashboard scans: the output base plus the extra ones.

        Watching the *base* directory rather than ``output_dir`` keeps previous
        sessions visible while the current one is still being written.
        """
        folders: list[Path] = []
        seen: set[Path] = set()
        for folder in (self.output_base_dir, *self.visualizer_additional_folders):
            key = folder.expanduser().resolve()
            if key in seen:
                continue
            seen.add(key)
            folders.append(folder)
        return folders

    @property
    def sync_enabled(self) -> bool:
        return self.rsync_dest is not None

    @property
    def max_shutter_seconds(self) -> float:
        """Slowest allowed exposure: ``min(30, interval - margin)``."""
        return min(
            CAMERA_MAX_SHUTTER_SECONDS,
            self.interval_seconds - self.shutter_margin_seconds,
        )

    @property
    def metadata_path(self) -> Path:
        return self.output_dir / self.metadata_filename

    @property
    def log_file_path(self) -> Path | None:
        """Destination for the session log file, or ``None`` when disabled."""
        return self.output_dir / self.log_filename if self.log_file else None


def _load_toml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _default_output_dir(now: datetime) -> Path:
    return Path(now.strftime("%Y-%m-%d-%H-%M-%S"))


def parse_log_rotate(value: Any) -> int | None:
    """Parse a rotation interval like ``10m``/``1h``/``4h``/``1d``; off -> None."""
    if value is False or value is None:
        return None
    text = str(value).strip().lower()
    if text in _DISABLED_WORDS:
        return None
    match = _DURATION_PATTERN.match(text)
    if match is None:
        raise ConfigError(
            f"log_rotate {value!r} must be a duration like 10m, 1h, 4h, 1d, or 'off'"
        )
    seconds = int(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]
    if seconds < MIN_LOG_ROTATE_SECONDS:
        raise ConfigError(
            f"log_rotate {value!r} is shorter than the minimum of 1m"
        )
    return seconds


@dataclass(frozen=True)
class _SyncSettings:
    rsync_dest: str | None = None
    rsync_ssh_key: Path | None = None
    rsync_ssh_port: int | None = None
    rsync_bwlimit: int | None = None
    rsync_timeout: float = DEFAULT_RSYNC_TIMEOUT_SECONDS
    delete_synced_raw: bool = False
    sync_backlog_limit: int = DEFAULT_SYNC_BACKLOG_LIMIT
    on_sync_backlog: str = "abort"
    output_dir_in_memory: bool = False


def _resolve_sync(top: Any, output_dir: Path) -> _SyncSettings:
    """Resolve the remote-sync settings and enforce the RAM-storage rules."""
    memory_mount = memory_mount_point(output_dir)
    in_memory = memory_mount is not None
    if in_memory and output_dir.expanduser().resolve() == memory_mount:
        raise ConfigError(
            f"--output-dir {output_dir} is the root of the RAM filesystem "
            f"{memory_mount}; use a sub-directory so the session owns its files "
            "(resume scans the capture directory for *.cr2)"
        )

    raw_dest = top("rsync_dest")
    dest = str(raw_dest).strip() if raw_dest is not None and str(raw_dest).strip() else None
    if in_memory and dest is None:
        raise ConfigError(
            f"--output-dir {output_dir} is on the RAM filesystem {memory_mount}, so "
            "CR2 files cannot be kept there. Pass --rsync-dest user@host:/path to "
            "stream each frame to a remote host and free it locally."
        )

    if dest is not None:
        try:
            RsyncDest.parse(dest)
        except SyncError as exc:
            raise ConfigError(str(exc)) from exc

    raw_key = top("rsync_ssh_key")
    key: Path | None = None
    if raw_key is not None and str(raw_key).strip():
        try:
            key = validate_ssh_key(Path(str(raw_key)))
        except SyncError as exc:
            raise ConfigError(str(exc)) from exc

    policy = str(top("on_sync_backlog", "abort")).strip().lower()
    if policy not in SYNC_BACKLOG_POLICIES:
        raise ConfigError(
            "on_sync_backlog must be one of: " + ", ".join(SYNC_BACKLOG_POLICIES)
        )

    backlog_limit = int(top("sync_backlog_limit", DEFAULT_SYNC_BACKLOG_LIMIT))
    if backlog_limit < 1:
        raise ConfigError(f"sync_backlog_limit {backlog_limit} must be at least 1")

    port = top("rsync_ssh_port")
    bwlimit = top("rsync_bwlimit")
    timeout = float(top("rsync_timeout", DEFAULT_RSYNC_TIMEOUT_SECONDS))
    if timeout <= 0:
        raise ConfigError(f"rsync_timeout {timeout:g} must be positive")

    # Deleting the local raw is the whole point on RAM storage, but elsewhere
    # rsync is a backup and the originals should stay put.
    delete_raw = top("delete_synced_raw")
    if delete_raw is None:
        delete_raw = in_memory
    if delete_raw and dest is None:
        raise ConfigError("--delete-synced-raw requires --rsync-dest")

    return _SyncSettings(
        rsync_dest=dest,
        rsync_ssh_key=key,
        rsync_ssh_port=int(port) if port is not None else None,
        rsync_bwlimit=int(bwlimit) if bwlimit is not None else None,
        rsync_timeout=timeout,
        delete_synced_raw=bool(delete_raw),
        sync_backlog_limit=backlog_limit,
        on_sync_backlog=policy,
        output_dir_in_memory=in_memory,
    )


# TOML key (under [visualizer]) -> CLI destination name.
_VISUALIZER_CLI_KEYS = {
    "enabled": "visualizer",
    "only": "visualizer_only",
    "host": "visualizer_host",
    "port": "visualizer_port",
    "open_browser": "visualizer_open_browser",
    "additional_folders": "additional_visualizer_folders",
}


@dataclass(frozen=True)
class _VisualizerSettings:
    enabled: bool = True
    only: bool = False
    host: str = DEFAULT_VISUALIZER_HOST
    port: int = DEFAULT_VISUALIZER_PORT
    open_browser: bool = False
    additional_folders: tuple[Path, ...] = ()


def _resolve_visualizer(vis: Any) -> _VisualizerSettings:
    """Resolve the dashboard settings. Must run before the capture validation."""
    only = bool(vis("only", False))

    try:
        port = int(vis("port", DEFAULT_VISUALIZER_PORT))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"visualizer port must be a number: {exc}") from exc
    # 0 means "any free port"; the bound URL is logged on start.
    if not 0 <= port <= 65535:
        raise ConfigError(f"visualizer port {port} must be between 0 and 65535")

    host = str(vis("host", DEFAULT_VISUALIZER_HOST)).strip() or DEFAULT_VISUALIZER_HOST

    # A capture box is usually headless, so only the dedicated dashboard mode
    # pops a browser open by itself.
    open_browser = vis("open_browser")
    if open_browser is None:
        open_browser = only

    raw_folders = vis("additional_folders") or ()
    if isinstance(raw_folders, (str, Path)):
        raw_folders = [raw_folders]
    folders = tuple(Path(str(folder)).expanduser() for folder in raw_folders)

    return _VisualizerSettings(
        enabled=bool(vis("enabled", True)),
        only=only,
        host=host,
        port=port,
        open_browser=bool(open_browser),
        additional_folders=folders,
    )


def build_config(
    cli: dict[str, Any],
    *,
    toml_path: Path | None = None,
    now: datetime | None = None,
) -> Config:
    """Merge ``cli`` overrides with the TOML file and return a :class:`Config`.

    ``cli`` values that are ``None`` mean "not supplied on the command line" and
    fall through to the TOML file / defaults.
    """
    now = now or datetime.now()
    toml_data = _load_toml(toml_path)
    location_toml = toml_data.get("location", {})
    ramp_toml = toml_data.get("ramping", {})
    visualizer_toml = toml_data.get("visualizer", {})

    def top(key: str, default: Any = None) -> Any:
        value = cli.get(key)
        return value if value is not None else toml_data.get(key, default)

    def loc(cli_key: str, toml_key: str, default: Any) -> Any:
        value = cli.get(cli_key)
        return value if value is not None else location_toml.get(toml_key, default)

    def ramp(toml_key: str, default: Any) -> Any:
        value = cli.get(f"ramp_{toml_key}")
        return value if value is not None else ramp_toml.get(toml_key, default)

    def vis(toml_key: str, default: Any = None) -> Any:
        value = cli.get(_VISUALIZER_CLI_KEYS[toml_key])
        return value if value is not None else visualizer_toml.get(toml_key, default)

    # Resolved first: dashboard-only runs never touch a camera, so they must
    # skip the capture preconditions below.
    viz = _resolve_visualizer(vis)

    camera_transport = str(top("camera_transport", DEFAULT_CAMERA_TRANSPORT)).strip().lower()
    if camera_transport not in {"ptpip", "usb"}:
        raise ConfigError(
            "camera_transport must be one of: ptpip, usb"
        )

    camera_address_default = (
        DEFAULT_CAMERA_ADDRESS if camera_transport == "ptpip" else None
    )
    camera_address = top("camera_address", camera_address_default)
    if camera_transport == "ptpip" and not viz.only:
        if camera_address is None or not str(camera_address).strip():
            raise ConfigError("camera_address is required when camera_transport=ptpip")
        camera_address_value: str | None = str(camera_address).strip()
    else:
        camera_address_value = (
            str(camera_address).strip() if camera_address is not None and str(camera_address).strip() else None
        )

    camera_selector = top("camera_selector")
    camera_selector_value = (
        str(camera_selector).strip() if camera_selector is not None and str(camera_selector).strip() else None
    )

    # --- interval (the only parameter with no default) ----------------------
    min_interval = float(top("min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS))
    interval = top("interval_seconds")
    if interval is None:
        if not viz.only:
            raise ConfigError(
                "required parameter 'interval_seconds' is missing from both the CLI "
                "(--interval) and the config file"
            )
        interval = min_interval
    interval = float(interval)

    margin = float(top("shutter_margin_seconds", DEFAULT_SHUTTER_MARGIN_SECONDS))
    if interval < min_interval:
        raise ConfigError(
            f"interval {interval:g}s is below the minimum {min_interval:g}s "
            "(the camera needs time to transfer each CR2)"
        )
    if interval <= margin:
        raise ConfigError(
            f"interval {interval:g}s must exceed the shutter margin {margin:g}s"
        )

    # --- location -----------------------------------------------------------
    defaults = LocationConfig()
    timezone = loc("timezone", "timezone", defaults.timezone)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"invalid timezone {timezone!r}: {exc}") from exc
    location = LocationConfig(
        latitude=float(loc("latitude", "latitude", defaults.latitude)),
        longitude=float(loc("longitude", "longitude", defaults.longitude)),
        timezone=timezone,
        elevation=float(loc("elevation", "elevation", defaults.elevation)),
        name=str(loc("location_name", "name", defaults.name)),
    )

    # --- ramping params -----------------------------------------------------
    rp_defaults = RampParams()
    max_shutter_seconds = min(CAMERA_MAX_SHUTTER_SECONDS, interval - margin)
    ramp_params = RampParams(
        target_headroom_stops=float(
            ramp("target_headroom_stops", rp_defaults.target_headroom_stops)
        ),
        deadband_stops=float(ramp("deadband_stops", rp_defaults.deadband_stops)),
        white_clip_pct_threshold=float(
            ramp("white_clip_pct_threshold", rp_defaults.white_clip_pct_threshold)
        ),
        black_clip_pct_threshold=float(
            ramp("black_clip_pct_threshold", rp_defaults.black_clip_pct_threshold)
        ),
        target_headroom_stops_night=float(
            ramp(
                "target_headroom_stops_night",
                rp_defaults.target_headroom_stops_night,
            )
        ),
        white_clip_pct_threshold_night=float(
            ramp(
                "white_clip_pct_threshold_night",
                rp_defaults.white_clip_pct_threshold_night,
            )
        ),
        black_clip_pct_limit=float(
            ramp("black_clip_pct_limit", rp_defaults.black_clip_pct_limit)
        ),
        day_altitude_deg=float(ramp("day_altitude_deg", rp_defaults.day_altitude_deg)),
        night_altitude_deg=float(
            ramp("night_altitude_deg", rp_defaults.night_altitude_deg)
        ),
        dawn_proactive_black_pct=float(
            ramp("dawn_proactive_black_pct", rp_defaults.dawn_proactive_black_pct)
        ),
        dawn_proactive_mean_ev_window=int(
            ramp(
                "dawn_proactive_mean_ev_window",
                rp_defaults.dawn_proactive_mean_ev_window,
            )
        ),
        highlight_percentile=float(
            ramp("highlight_percentile", rp_defaults.highlight_percentile)
        ),
        white_clip_frac=float(ramp("white_clip_frac", rp_defaults.white_clip_frac)),
        black_clip_frac=float(ramp("black_clip_frac", rp_defaults.black_clip_frac)),
        half_size_decode=bool(ramp("half_size_decode", rp_defaults.half_size_decode)),
        trend_window=int(ramp("trend_window", rp_defaults.trend_window)),
        trend_ev_threshold=float(
            ramp("trend_ev_threshold", rp_defaults.trend_ev_threshold)
        ),
        iso_dark_limit=str(ramp("iso_dark_limit", rp_defaults.iso_dark_limit)),
        iso_bright_limit=str(ramp("iso_bright_limit", rp_defaults.iso_bright_limit)),
        aperture_dark_limit=str(
            ramp("aperture_dark_limit", rp_defaults.aperture_dark_limit)
        ),
        aperture_bright_limit=str(
            ramp("aperture_bright_limit", rp_defaults.aperture_bright_limit)
        ),
        shutter_dark_limit=str(
            ramp("shutter_dark_limit", rp_defaults.shutter_dark_limit)
        ),
        max_shutter_seconds=max_shutter_seconds,
    )

    # --- output dir (CLI only) ---------------------------------------------
    create_timestamped_subdirectory = bool(
        top("create_timestamped_subdirectory", True)
    )
    daemon = bool(top("daemon", False))
    if daemon and viz.only:
        raise ConfigError(
            "--visualizer-only cannot be combined with --daemon: there is no "
            "camera session to supervise"
        )
    camera_lost_cycles = float(top("camera_lost_cycles", DEFAULT_CAMERA_LOST_CYCLES))
    if camera_lost_cycles < 1:
        raise ConfigError(
            f"camera_lost_cycles {camera_lost_cycles:g} must be at least 1"
        )
    daemon_poll_seconds = float(
        top("daemon_poll_seconds", DEFAULT_DAEMON_POLL_SECONDS)
    )
    if daemon_poll_seconds <= 0:
        raise ConfigError(
            f"daemon_poll_seconds {daemon_poll_seconds:g} must be positive"
        )

    output_dir_value = cli.get("output_dir")
    if daemon and output_dir_value is None:
        raise ConfigError(
            "--output-dir is required in daemon mode (the default directory is "
            "relative to the working directory, which a service manager does "
            "not set predictably)"
        )
    if daemon and not create_timestamped_subdirectory:
        raise ConfigError(
            "daemon mode requires create_timestamped_subdirectory=true so each "
            "session gets its own folder instead of resuming the previous one"
        )
    if output_dir_value is None:
        # No --output-dir: always work in a fresh timestamped folder under CWD,
        # so the dashboard watches the CWD to still see every session.
        output_base_dir = Path(".")
        output_dir = _default_output_dir(now)
    else:
        output_base_dir = Path(output_dir_value).expanduser()
        # With a timestamped subdirectory, the run lives in a uniquely-named
        # folder inside --output-dir (same name as the no-output-dir default);
        # otherwise --output-dir is used directly as the working directory.
        output_dir = (
            output_base_dir / _default_output_dir(now)
            if create_timestamped_subdirectory
            else output_base_dir
        )

    # --- remote sync --------------------------------------------------------
    # Dashboard-only runs write nothing, so the RAM-storage rules do not apply.
    sync = _SyncSettings() if viz.only else _resolve_sync(top, output_dir)

    return Config(
        camera_transport=camera_transport,
        camera_address=camera_address_value,
        camera_selector=camera_selector_value,
        interval_seconds=interval,
        output_dir=output_dir,
        location=location,
        ramp=ramp_params,
        auto_init=bool(top("auto_init", False)),
        auto_init_keep_files=bool(
            top("auto_init_keep_files", not sync.output_dir_in_memory)
        ),
        create_timestamped_subdirectory=create_timestamped_subdirectory,
        long_exposure_nr=bool(top("long_exposure_nr", True)),
        shutter_margin_seconds=margin,
        min_interval_seconds=min_interval,
        log_level_stdout=str(top("log_level_stdout", "INFO")).upper(),
        log_file=bool(top("log_file", True)),
        log_level_file=str(top("log_level_file", "DEBUG")).upper(),
        log_rotate_seconds=parse_log_rotate(top("log_rotate", DEFAULT_LOG_ROTATE)),
        capture_retries=int(top("capture_retries", 5)),
        download_retries=int(top("download_retries", 3)),
        daemon=daemon,
        camera_lost_cycles=camera_lost_cycles,
        daemon_poll_seconds=daemon_poll_seconds,
        rsync_dest=sync.rsync_dest,
        rsync_ssh_key=sync.rsync_ssh_key,
        rsync_ssh_port=sync.rsync_ssh_port,
        rsync_bwlimit=sync.rsync_bwlimit,
        rsync_timeout=sync.rsync_timeout,
        delete_synced_raw=sync.delete_synced_raw,
        sync_backlog_limit=sync.sync_backlog_limit,
        on_sync_backlog=sync.on_sync_backlog,
        output_dir_in_memory=sync.output_dir_in_memory,
        output_base_dir=output_base_dir,
        visualizer=viz.enabled,
        visualizer_only=viz.only,
        visualizer_host=viz.host,
        visualizer_port=viz.port,
        visualizer_open_browser=viz.open_browser,
        visualizer_additional_folders=viz.additional_folders,
    )
