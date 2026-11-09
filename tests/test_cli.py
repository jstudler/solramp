# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the argparse CLI and its mapping onto build_config."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from solramp import cli
from solramp.config import Config, build_config

NOW = datetime(2024, 6, 21, 18, 0, 0)


def _config_from_args(argv: list[str]) -> Config:
    args = cli.build_parser().parse_args(argv)
    overrides = vars(args)
    overrides.pop("config", None)
    return build_config(overrides, now=NOW)


def test_cli_values_flow_into_config() -> None:
    config = _config_from_args(
        [
            "--interval", "30",
            "--camera-address", "1.2.3.4",
            "--auto-init",
            "--target-headroom-stops", "1.5",
            "--no-half-size-decode",
            "--latitude", "40",
            "--iso-bright-limit", "6400",
        ]
    )
    assert config.interval_seconds == 30
    assert config.camera_address == "1.2.3.4"
    assert config.auto_init is True
    assert config.ramp.target_headroom_stops == pytest.approx(1.5)
    assert config.ramp.half_size_decode is False
    assert config.location.latitude == pytest.approx(40.0)
    assert config.ramp.iso_bright_limit == "6400"


def test_absent_flags_fall_through_to_defaults() -> None:
    config = _config_from_args(["--interval", "30"])
    assert config.auto_init is False  # --auto-init absent -> default
    assert config.ramp.half_size_decode is True  # decode flag absent -> default


def test_half_size_can_be_explicitly_enabled() -> None:
    config = _config_from_args(["--interval", "30", "--half-size-decode"])
    assert config.ramp.half_size_decode is True


def test_cli_transport_and_selector_flow_into_config() -> None:
    config = _config_from_args(
        [
            "--interval", "30",
            "--camera-transport", "usb",
            "--camera-selector", "001,002",
        ]
    )
    assert config.camera_transport == "usb"
    assert config.camera_selector == "001,002"
    assert config.camera_address is None


def test_main_returns_2_on_config_error(caplog: pytest.LogCaptureFixture) -> None:
    # No interval anywhere -> ConfigError -> exit code 2, no camera access.
    assert cli.main([]) == 2


def test_main_invokes_run_with_built_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Config] = {}

    def fake_run(config: Config) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    rc = cli.main(["--interval", "30", "--camera-address", "9.9.9.9"])
    assert rc == 0
    assert captured["config"].camera_address == "9.9.9.9"
    assert captured["config"].interval_seconds == 30


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0


def test_daemon_flag_dispatches_to_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_supervise(cli_overrides: dict, toml_path: Path | None) -> int:
        captured["overrides"] = cli_overrides
        return 0

    def fail_run(config: Config) -> int:  # pragma: no cover - must not be called
        raise AssertionError("daemon mode must not call run() directly")

    monkeypatch.setattr(cli, "supervise", fake_supervise)
    monkeypatch.setattr(cli, "run", fail_run)

    rc = cli.main(
        ["--interval", "30", "--daemon", "--output-dir", str(tmp_path),
         "--camera-address", "9.9.9.9"]
    )

    assert rc == 0
    assert captured["overrides"]["daemon"] is True


def test_daemon_without_output_dir_is_a_config_error() -> None:
    assert cli.main(["--interval", "30", "--daemon"]) == 2


def test_visualizer_flags_flow_into_config(tmp_path: Path) -> None:
    config = _config_from_args(
        [
            "--interval", "30",
            "--no-visualizer",
            "--visualizer-host", "0.0.0.0",
            "--visualizer-port", "9001",
            "--output-dir", "captures",
            "--additional-visualizer-folders", "archive", str(tmp_path),
        ]
    )
    assert config.visualizer is False
    assert config.visualizer_host == "0.0.0.0"
    assert config.visualizer_port == 9001
    assert config.visualizer_watch_folders == [
        Path("captures"), Path("archive"), tmp_path
    ]


def test_visualizer_only_dispatches_to_the_dashboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Config] = {}

    def fake_visualizer(config: Config) -> int:
        captured["config"] = config
        return 0

    def fail(*_args: object, **_kw: object) -> int:  # pragma: no cover
        raise AssertionError("visualizer-only mode must not touch the camera")

    monkeypatch.setattr(cli, "run_visualizer_only", fake_visualizer)
    monkeypatch.setattr(cli, "run", fail)
    monkeypatch.setattr(cli, "supervise", fail)

    # No --interval: visualizer-only mode must not require one.
    rc = cli.main(["--visualizer-only", "--output-dir", str(tmp_path)])

    assert rc == 0
    assert captured["config"].visualizer_only is True
    assert captured["config"].visualizer_watch_folders == [tmp_path]
