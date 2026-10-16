# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for configuration merging, precedence and validation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from solramp.config import (
    DEFAULT_CAMERA_ADDRESS,
    Config,
    ConfigError,
    build_config,
    is_memory_backed,
)

NOW = datetime(2024, 6, 21, 18, 0, 0)


def _build(cli: dict, **kw) -> Config:
    return build_config(cli, now=NOW, **kw)


def test_minimal_config_uses_defaults() -> None:
    config = _build({"interval_seconds": 30})
    assert config.interval_seconds == 30
    assert config.camera_transport == "ptpip"
    assert config.camera_address == DEFAULT_CAMERA_ADDRESS
    assert config.camera_selector is None
    assert config.auto_init is False
    assert config.auto_init_keep_files is True
    assert config.create_timestamped_subdirectory is True
    assert config.log_level_stdout == "INFO"
    assert config.log_file is True
    assert config.log_level_file == "DEBUG"
    assert config.log_rotate_seconds == 3600
    # Default output dir derives from `now`.
    assert config.output_dir == Path("2024-06-21-18-00-00")


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("10m", 600), ("1h", 3600), ("4h", 14400), ("1d", 86400), ("90s", 90),
     (" 2H ", 7200), ("off", None), ("none", None), (False, None)],
)
def test_log_rotate_parses_durations(value: object, seconds: int | None) -> None:
    config = _build({"interval_seconds": 30, "log_rotate": value})
    assert config.log_rotate_seconds == seconds


def test_log_rotate_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "c.toml"
    toml.write_text('interval_seconds = 30\nlog_rotate = "4h"\n', encoding="utf-8")
    assert _build({}, toml_path=toml).log_rotate_seconds == 14400


@pytest.mark.parametrize("value", ["30s", "1w", "h", "1.5h", "-1h"])
def test_log_rotate_rejects_bad_values(value: str) -> None:
    with pytest.raises(ConfigError, match="log_rotate"):
        _build({"interval_seconds": 30, "log_rotate": value})


def test_interval_is_required() -> None:
    with pytest.raises(ConfigError, match="interval_seconds"):
        _build({})


def test_interval_below_minimum_rejected() -> None:
    with pytest.raises(ConfigError, match="minimum"):
        _build({"interval_seconds": 10})


def test_interval_must_exceed_margin() -> None:
    with pytest.raises(ConfigError, match="margin"):
        _build({"interval_seconds": 20, "min_interval_seconds": 1, "shutter_margin_seconds": 25})


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ConfigError, match="timezone"):
        _build({"interval_seconds": 30, "timezone": "Mars/Olympus"})


def test_max_shutter_is_interval_minus_margin_capped_at_30() -> None:
    short = _build({"interval_seconds": 25, "shutter_margin_seconds": 5})
    assert short.max_shutter_seconds == pytest.approx(20.0)
    long = _build({"interval_seconds": 120, "shutter_margin_seconds": 5})
    assert long.max_shutter_seconds == pytest.approx(30.0)  # camera ceiling


def test_cli_overrides_take_precedence_over_toml(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text(
        "interval_seconds = 25\n"
        'camera_address = "10.0.0.5"\n'
        'log_level_stdout = "warning"\n'
        "[location]\n"
        "latitude = 10.0\n"
    )
    # CLI interval wins; camera_address falls through to TOML.
    config = _build({"interval_seconds": 45}, toml_path=toml)
    assert config.interval_seconds == 45
    assert config.camera_address == "10.0.0.5"
    assert config.log_level_stdout == "WARNING"
    assert config.location.latitude == pytest.approx(10.0)


def test_output_dir_is_cli_only(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text('interval_seconds = 30\noutput_dir = "ignored_from_toml"\n')
    # TOML output_dir must be ignored; falls back to the timestamp default.
    config = _build({}, toml_path=toml)
    assert config.output_dir == Path("2024-06-21-18-00-00")
    # CLI value is honoured; by default a timestamped subfolder is created in it.
    config = _build({"output_dir": "my_run"}, toml_path=toml)
    assert config.output_dir == Path("my_run") / "2024-06-21-18-00-00"


def test_create_timestamped_subdirectory_true_nests_run(tmp_path: Path) -> None:
    config = _build({"interval_seconds": 30, "output_dir": "my_run"})
    assert config.output_dir == Path("my_run") / "2024-06-21-18-00-00"


def test_create_timestamped_subdirectory_false_uses_output_dir_directly() -> None:
    config = _build(
        {
            "interval_seconds": 30,
            "output_dir": "my_run",
            "create_timestamped_subdirectory": False,
        }
    )
    assert config.output_dir == Path("my_run")


def test_create_timestamped_subdirectory_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text(
        "interval_seconds = 30\ncreate_timestamped_subdirectory = false\n"
    )
    config = _build({"output_dir": "my_run"}, toml_path=toml)
    assert config.output_dir == Path("my_run")


def test_create_timestamped_subdirectory_no_effect_without_output_dir() -> None:
    config = _build(
        {"interval_seconds": 30, "create_timestamped_subdirectory": False}
    )
    assert config.output_dir == Path("2024-06-21-18-00-00")


def test_auto_init_keep_files_default_and_override() -> None:
    assert _build({"interval_seconds": 30}).auto_init_keep_files is True
    config = _build({"interval_seconds": 30, "auto_init_keep_files": False})
    assert config.auto_init_keep_files is False


# --------------------------------------------------------------------------
# Visualizer
# --------------------------------------------------------------------------
def test_visualizer_defaults() -> None:
    config = _build({"interval_seconds": 30})
    assert config.visualizer is True
    assert config.visualizer_only is False
    assert config.visualizer_host == "127.0.0.1"
    assert config.visualizer_port == 8000
    # Capture boxes are usually headless.
    assert config.visualizer_open_browser is False
    assert config.visualizer_additional_folders == ()


def test_visualizer_can_be_disabled() -> None:
    assert _build({"interval_seconds": 30, "visualizer": False}).visualizer is False


def test_watch_folder_is_the_output_base_not_the_session_folder() -> None:
    config = _build({"interval_seconds": 30, "output_dir": "captures"})
    assert config.output_dir == Path("captures") / "2024-06-21-18-00-00"
    assert config.output_base_dir == Path("captures")
    assert config.visualizer_watch_folders == [Path("captures")]


def test_watch_folder_without_output_dir_is_the_cwd() -> None:
    config = _build({"interval_seconds": 30})
    assert config.visualizer_watch_folders == [Path(".")]


def test_additional_watch_folders_are_appended_and_deduplicated() -> None:
    config = _build(
        {
            "interval_seconds": 30,
            "output_dir": "captures",
            "additional_visualizer_folders": ["archive", "captures"],
        }
    )
    assert config.visualizer_watch_folders == [Path("captures"), Path("archive")]


def test_visualizer_only_does_not_need_an_interval_or_camera() -> None:
    config = _build({"visualizer_only": True, "camera_address": ""})
    assert config.visualizer_only is True
    assert config.camera_address is None
    # Dashboard-only mode opens a browser; a capture run does not.
    assert config.visualizer_open_browser is True


def test_visualizer_only_skips_the_ram_storage_rules(tmp_path: Path) -> None:
    # A capture run into RAM would demand --rsync-dest; the dashboard writes nothing.
    config = _build({"visualizer_only": True, "output_dir": "/dev/shm/solramp"})
    assert config.rsync_dest is None
    assert config.visualizer_watch_folders == [Path("/dev/shm/solramp")]


def test_visualizer_only_and_daemon_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="visualizer-only"):
        _build(
            {
                "interval_seconds": 30,
                "daemon": True,
                "visualizer_only": True,
                "output_dir": str(tmp_path),
            }
        )


def test_visualizer_port_out_of_range_rejected() -> None:
    with pytest.raises(ConfigError, match="visualizer port"):
        _build({"interval_seconds": 30, "visualizer_port": 70000})


def test_visualizer_settings_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text(
        "interval_seconds = 30\n"
        "[visualizer]\n"
        "enabled = false\n"
        'host = "0.0.0.0"\n'
        "port = 9001\n"
        'additional_folders = ["extra"]\n'
    )
    config = _build({}, toml_path=toml)
    assert config.visualizer is False
    assert config.visualizer_host == "0.0.0.0"
    assert config.visualizer_port == 9001
    assert config.visualizer_additional_folders == (Path("extra"),)

    # CLI still wins over the file.
    config = _build({"visualizer_port": 9100}, toml_path=toml)
    assert config.visualizer_port == 9100


def test_ramp_params_from_cli() -> None:
    config = _build(
        {
            "interval_seconds": 30,
            "ramp_target_headroom_stops": 1.5,
            "ramp_half_size_decode": False,
            "ramp_iso_bright_limit": "6400",
        }
    )
    assert config.ramp.target_headroom_stops == pytest.approx(1.5)
    assert config.ramp.half_size_decode is False
    assert config.ramp.iso_bright_limit == "6400"


def test_ramp_params_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text(
        "interval_seconds = 30\n[ramping]\ntarget_headroom_stops = 2.0\ndeadband_stops = 0.5\n"
    )
    config = _build({}, toml_path=toml)
    assert config.ramp.target_headroom_stops == pytest.approx(2.0)
    assert config.ramp.deadband_stops == pytest.approx(0.5)


def test_missing_toml_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        _build({"interval_seconds": 30}, toml_path=tmp_path / "nope.toml")


def test_location_from_cli() -> None:
    config = _build(
        {
            "interval_seconds": 30,
            "latitude": 40.0,
            "longitude": -3.7,
            "timezone": "Europe/Madrid",
            "location_name": "Madrid",
        }
    )
    assert config.location.latitude == pytest.approx(40.0)
    assert config.location.timezone == "Europe/Madrid"
    assert config.location.name == "Madrid"


def test_usb_transport_allows_missing_address() -> None:
    config = _build({"interval_seconds": 30, "camera_transport": "usb"})
    assert config.camera_transport == "usb"
    assert config.camera_address is None


def test_invalid_camera_transport_rejected() -> None:
    with pytest.raises(ConfigError, match="camera_transport"):
        _build({"interval_seconds": 30, "camera_transport": "bluetooth"})


def test_ptpip_requires_camera_address() -> None:
    with pytest.raises(ConfigError, match="camera_address"):
        _build({"interval_seconds": 30, "camera_transport": "ptpip", "camera_address": ""})


# --------------------------------------------------------------------------
# Daemon mode
# --------------------------------------------------------------------------
def test_daemon_defaults() -> None:
    config = _build({"interval_seconds": 30})
    assert config.daemon is False
    assert config.camera_lost_cycles == 5.0
    assert config.daemon_poll_seconds == 15.0


def test_daemon_options_from_cli(tmp_path: Path) -> None:
    config = _build(
        {
            "interval_seconds": 30,
            "output_dir": str(tmp_path),
            "daemon": True,
            "camera_lost_cycles": 3,
            "daemon_poll_seconds": 5,
        }
    )
    assert config.daemon is True
    assert config.camera_lost_cycles == 3
    assert config.daemon_poll_seconds == 5


def test_daemon_requires_output_dir() -> None:
    with pytest.raises(ConfigError, match="output-dir"):
        _build({"interval_seconds": 30, "daemon": True})


def test_daemon_rejects_shared_output_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="create_timestamped_subdirectory"):
        _build(
            {
                "interval_seconds": 30,
                "output_dir": str(tmp_path),
                "daemon": True,
                "create_timestamped_subdirectory": False,
            }
        )


def test_camera_lost_cycles_must_be_at_least_one() -> None:
    with pytest.raises(ConfigError, match="camera_lost_cycles"):
        _build({"interval_seconds": 30, "camera_lost_cycles": 0.5})


def test_daemon_poll_seconds_must_be_positive() -> None:
    with pytest.raises(ConfigError, match="daemon_poll_seconds"):
        _build({"interval_seconds": 30, "daemon_poll_seconds": 0})


# --------------------------------------------------------------------------
# RAM-backed capture directories and remote sync
# --------------------------------------------------------------------------
MOUNTS = (
    "/dev/sda1 / ext4 rw,relatime 0 0\n"
    "tmpfs /mnt/capture tmpfs rw,size=1500m 0 0\n"
    "/dev/sdb1 /mnt/capture/durable ext4 rw 0 0\n"
    "tmpfs /mnt/with\\040space tmpfs rw 0 0\n"
)


def test_memory_backed_detects_dev_shm_without_procfs() -> None:
    assert is_memory_backed(Path("/dev/shm/solramp/run"), mounts_text="")
    assert not is_memory_backed(Path("/srv/captures"), mounts_text="")


def test_memory_backed_detects_a_dedicated_tmpfs_mount() -> None:
    assert is_memory_backed(Path("/mnt/capture/run1"), mounts_text=MOUNTS)


def test_memory_backed_respects_a_durable_mount_inside_a_tmpfs() -> None:
    # Longest match wins across all filesystems, not just the RAM ones.
    assert not is_memory_backed(Path("/mnt/capture/durable/run1"), mounts_text=MOUNTS)


def test_memory_backed_unescapes_mount_points() -> None:
    assert is_memory_backed(Path("/mnt/with space/run"), mounts_text=MOUNTS)


def test_ram_output_dir_requires_an_rsync_destination() -> None:
    with pytest.raises(ConfigError, match="RAM filesystem"):
        _build({"interval_seconds": 30, "output_dir": "/dev/shm/solramp"})


def test_ram_output_dir_rejects_the_mount_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="root of the RAM filesystem"):
        _build(
            {
                "interval_seconds": 30,
                "output_dir": "/dev/shm",
                "create_timestamped_subdirectory": False,
                "rsync_dest": str(tmp_path),
            }
        )


def test_ram_output_dir_with_dest_enables_deletion_and_drops_autoinit_files(
    tmp_path: Path,
) -> None:
    config = _build(
        {
            "interval_seconds": 30,
            "output_dir": "/dev/shm/solramp",
            "rsync_dest": "pi@backup:/srv/tl",
        }
    )
    assert config.output_dir_in_memory is True
    assert config.sync_enabled is True
    assert config.delete_synced_raw is True
    # Auto-init frames are never swept, so they would pin RAM all session.
    assert config.auto_init_keep_files is False


def test_durable_output_dir_keeps_raws_even_with_a_destination(tmp_path: Path) -> None:
    config = _build(
        {
            "interval_seconds": 30,
            "output_dir": str(tmp_path),
            "rsync_dest": "pi@backup:/srv/tl",
        }
    )
    assert config.output_dir_in_memory is False
    assert config.delete_synced_raw is False
    assert config.auto_init_keep_files is True


def test_no_destination_means_sync_disabled(tmp_path: Path) -> None:
    config = _build({"interval_seconds": 30, "output_dir": str(tmp_path)})
    assert config.sync_enabled is False
    assert config.rsync_dest is None


def test_invalid_rsync_destination_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="daemon syntax"):
        _build(
            {
                "interval_seconds": 30,
                "output_dir": str(tmp_path),
                "rsync_dest": "host::module",
            }
        )


def test_unusable_ssh_key_rejected(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("private", encoding="utf-8")
    key.chmod(0o644)
    with pytest.raises(ConfigError, match="group/world accessible"):
        _build(
            {
                "interval_seconds": 30,
                "output_dir": str(tmp_path),
                "rsync_dest": "host:/srv/tl",
                "rsync_ssh_key": key,
            }
        )


def test_delete_synced_raw_requires_a_destination(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="requires --rsync-dest"):
        _build(
            {
                "interval_seconds": 30,
                "output_dir": str(tmp_path),
                "delete_synced_raw": True,
            }
        )


def test_invalid_backlog_policy_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="on_sync_backlog"):
        _build(
            {
                "interval_seconds": 30,
                "output_dir": str(tmp_path),
                "on_sync_backlog": "panic",
            }
        )


def test_sync_settings_can_come_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text(
        'rsync_dest = "pi@backup:/srv/tl"\nrsync_bwlimit = 400\n',
        encoding="utf-8",
    )
    config = _build(
        {"interval_seconds": 30, "output_dir": str(tmp_path)}, toml_path=toml
    )
    assert config.rsync_dest == "pi@backup:/srv/tl"
    assert config.rsync_bwlimit == 400
