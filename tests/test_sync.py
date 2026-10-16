# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the remote-sync destination parsing, argv building and the
ship-then-free loop.

The integration tests point the destination at a *local directory*: rsync skips
ssh entirely for a local transfer, so the real binary still exercises
``--remove-source-files``, batching and the "side-cars survive, raws vanish"
invariant without needing a server.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from solramp.config import build_config
from solramp.sync import (
    RemoteSync,
    RsyncDest,
    SyncError,
    build_rsync_argv,
    ssh_command,
    validate_ssh_key,
)

NOW = datetime(2024, 6, 21, 18, 0, 0)
SESSION = "2024-06-21-18-00-00"

needs_rsync = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync is not installed"
)


# --------------------------------------------------------------------------
# Destination parsing
# --------------------------------------------------------------------------
def test_parse_user_host_path() -> None:
    dest = RsyncDest.parse("pi@backup.example:/srv/timelapse")
    assert (dest.user, dest.host, dest.path) == ("pi", "backup.example", "/srv/timelapse")
    assert not dest.is_local
    assert dest.ssh_target == "pi@backup.example"
    assert dest.target(SESSION) == f"pi@backup.example:/srv/timelapse/{SESSION}/"


def test_parse_ssh_config_alias_without_user() -> None:
    dest = RsyncDest.parse("tl-backup:/srv/tl")
    assert dest.user is None
    assert dest.ssh_target == "tl-backup"
    assert dest.target(SESSION) == f"tl-backup:/srv/tl/{SESSION}/"


def test_parse_ipv6_literal_keeps_brackets_in_target() -> None:
    dest = RsyncDest.parse("[2001:db8::1]:/srv/tl")
    assert dest.host == "2001:db8::1"
    assert dest.target(SESSION) == f"[2001:db8::1]:/srv/tl/{SESSION}/"


def test_parse_local_directory(tmp_path: Path) -> None:
    dest = RsyncDest.parse(str(tmp_path))
    assert dest.is_local
    assert dest.target(SESSION) == f"{tmp_path}/{SESSION}/"


def test_parse_rejects_missing_local_directory(tmp_path: Path) -> None:
    with pytest.raises(SyncError, match="existing local directory"):
        RsyncDest.parse(str(tmp_path / "nope"))


def test_parse_rejects_rsync_daemon_syntax() -> None:
    with pytest.raises(SyncError, match="daemon syntax"):
        RsyncDest.parse("host::module")


def test_parse_rejects_url_scheme() -> None:
    with pytest.raises(SyncError, match="URL scheme"):
        RsyncDest.parse("rsync://host/module")


def test_parse_rejects_tilde_path() -> None:
    # --secluded-args stops the remote shell expanding '~'.
    with pytest.raises(SyncError, match="absolute remote path"):
        RsyncDest.parse("host:~/captures")


def test_parse_rejects_empty() -> None:
    with pytest.raises(SyncError, match="empty"):
        RsyncDest.parse("   ")


# --------------------------------------------------------------------------
# ssh / rsync argv
# --------------------------------------------------------------------------
def test_ssh_command_is_non_interactive_and_keeps_host_key_checking() -> None:
    argv = ssh_command()
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    # Disabling host-key checking on an untrusted 5G link would be a MITM hole.
    assert not any("StrictHostKeyChecking" in arg for arg in argv)
    assert not any("UserKnownHostsFile" in arg for arg in argv)


def test_ssh_command_multiplexes_when_given_a_control_path(tmp_path: Path) -> None:
    argv = ssh_command(control_path=tmp_path / "cm", key=tmp_path / "k", port=2222)
    assert "ControlMaster=auto" in argv
    assert f"ControlPath={tmp_path / 'cm'}" in argv
    assert "IdentitiesOnly=yes" in argv
    assert argv[argv.index("-p") + 1] == "2222"


def test_rsync_argv_defaults_are_resumable_and_uncompressed() -> None:
    argv = build_rsync_argv([Path("/tmp/a.cr2")], "host:/dst/")
    assert argv[0] == "rsync"
    # -a would imply -og and fail on a non-root receiver; -z wastes Pi CPU on
    # already-compressed CR2s.
    assert "-rt" in argv and "-a" not in argv and "-z" not in argv
    assert "--partial-dir=.rsync-partial" in argv
    assert any(arg.startswith("--timeout=") for arg in argv)
    assert "--remove-source-files" not in argv
    assert argv[-2:] == ["/tmp/a.cr2", "host:/dst/"]


def test_rsync_argv_remove_source_files_and_bwlimit() -> None:
    argv = build_rsync_argv(
        [Path("/tmp/a.cr2")], "host:/dst/", remove_source_files=True, bwlimit=500
    )
    assert "--remove-source-files" in argv
    assert "--bwlimit=500" in argv


def test_rsync_argv_append_mode_drops_partial_dir() -> None:
    # --append implies --inplace, which conflicts with --partial-dir.
    argv = build_rsync_argv([Path("/tmp/x.log")], "host:/dst/", append=True)
    assert "--append" in argv and "--inplace" in argv
    assert not any(arg.startswith("--partial-dir") for arg in argv)


def test_rsync_argv_passes_ssh_command() -> None:
    argv = build_rsync_argv([Path("/tmp/a")], "host:/dst/", ssh="ssh -o BatchMode=yes")
    assert argv[argv.index("-e") + 1] == "ssh -o BatchMode=yes"


# --------------------------------------------------------------------------
# ssh key validation
# --------------------------------------------------------------------------
def test_validate_ssh_key_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(SyncError, match="does not exist"):
        validate_ssh_key(tmp_path / "id_ed25519")


def test_validate_ssh_key_rejects_world_readable(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("private", encoding="utf-8")
    key.chmod(0o644)
    with pytest.raises(SyncError, match="group/world accessible"):
        validate_ssh_key(key)


def test_validate_ssh_key_accepts_0600(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("private", encoding="utf-8")
    key.chmod(0o600)
    assert validate_ssh_key(key) == key


# --------------------------------------------------------------------------
# End-to-end against a local destination (real rsync, no ssh)
# --------------------------------------------------------------------------
def _syncer(tmp_path: Path, **overrides) -> RemoteSync:
    remote = tmp_path / "remote"
    remote.mkdir()
    cli = {
        "interval_seconds": 30,
        "output_dir": str(tmp_path / "capture"),
        "rsync_dest": str(remote),
        "delete_synced_raw": True,
        **overrides,
    }
    config = build_config(cli, now=NOW)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    syncer = RemoteSync.from_config(config)
    assert syncer is not None
    return syncer


def _write(path: Path, size: int = 2048) -> Path:
    path.write_bytes(b"\xff" * size)
    return path


@needs_rsync
def test_ship_uploads_raws_and_frees_them_but_keeps_sidecars(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path, log_rotate="off")
    syncer.preflight()
    out = syncer.config.output_dir
    raws = [_write(out / f"2024-06-21_18-00-{n:02d}.cr2") for n in (10, 11)]
    _write(syncer.config.metadata_path, 64)
    _write(syncer.config.log_file_path, 64)

    syncer._ship(list(raws))

    remote = Path(syncer.dest.remote_dir(syncer.session))
    for raw in raws:
        assert not raw.exists(), "synced raw must be freed locally"
        assert (remote / raw.name).is_file()
    # Resume needs these locally; they must never be removed.
    assert syncer.config.metadata_path.is_file()
    assert syncer.config.log_file_path.is_file()
    assert (remote / syncer.config.metadata_path.name).is_file()
    assert (remote / syncer.config.log_file_path.name).is_file()
    assert syncer.pending() == 0


@needs_rsync
def test_raws_are_kept_locally_when_deletion_is_disabled(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path, delete_synced_raw=False)
    syncer.preflight()
    raw = _write(syncer.config.output_dir / "2024-06-21_18-00-10.cr2")

    syncer._ship([raw])

    assert raw.is_file()
    assert (Path(syncer.dest.remote_dir(syncer.session)) / raw.name).is_file()
    assert syncer.pending() == 0


@needs_rsync
def test_worker_thread_drains_the_queue(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path)
    syncer.preflight()
    raws = [_write(syncer.config.output_dir / f"f{n}.cr2") for n in range(3)]
    syncer.start()
    for raw in raws:
        syncer.enqueue(raw)
    syncer.stop()
    syncer.join(timeout=30.0)

    remote = Path(syncer.dest.remote_dir(syncer.session))
    assert all(not raw.exists() for raw in raws)
    assert sorted(p.name for p in remote.glob("*.cr2")) == sorted(
        raw.name for raw in raws
    )


@needs_rsync
def test_sync_log_names_every_frame_and_its_duration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    syncer = _syncer(tmp_path)
    syncer.preflight()
    out = syncer.config.output_dir
    raws = [_write(out / f"2024-06-21_18-00-{n:02d}.cr2") for n in (10, 11)]

    with caplog.at_level(logging.INFO):
        syncer._ship(list(raws))

    line = next(m for m in caplog.messages if m.startswith("Synced frame(s)"))
    assert "2024-06-21_18-00-10.cr2, 2024-06-21_18-00-11.cr2" in line
    assert re.search(r"in \d+ms$", line)


@needs_rsync
def test_autoinit_frames_ship_into_their_subdirectory(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path)
    syncer.preflight()
    out = syncer.config.output_dir
    autoinit = out / ".autoinit"
    autoinit.mkdir()
    probe = _write(autoinit / "init_00.cr2")
    frame = _write(out / "2024-06-21_18-00-10.cr2")

    syncer._ship([probe, frame])

    remote = Path(syncer.dest.remote_dir(syncer.session))
    assert (remote / ".autoinit" / "init_00.cr2").is_file()
    assert (remote / frame.name).is_file()
    assert not (remote / "init_00.cr2").exists()
    # Freed like any other shipped raw.
    assert not probe.exists() and not frame.exists()
    assert syncer.pending() == 0


@needs_rsync
def test_autoinit_frames_flow_through_the_worker(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path, delete_synced_raw=False)
    syncer.preflight()
    autoinit = syncer.config.output_dir / ".autoinit"
    autoinit.mkdir()
    probe = _write(autoinit / "init_00.cr2")

    syncer.start()
    syncer.enqueue(probe)
    syncer.drain()
    syncer.start()  # the pipeline starting it again must be a no-op
    syncer.stop()
    syncer.join(timeout=30.0)

    remote = Path(syncer.dest.remote_dir(syncer.session))
    assert (remote / ".autoinit" / "init_00.cr2").is_file()
    assert probe.is_file()


def test_join_without_a_running_worker_does_not_block(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path)
    syncer.enqueue(syncer.config.output_dir / "never.cr2")
    syncer.stop()
    syncer.join(timeout=1.0)


@needs_rsync
def test_preflight_fails_on_an_unwritable_destination(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path)
    remote = Path(syncer.dest.path)
    remote.chmod(0o500)
    try:
        with pytest.raises(SyncError):
            syncer.preflight()
    finally:
        remote.chmod(0o700)


# --------------------------------------------------------------------------
# Backpressure
# --------------------------------------------------------------------------
def test_backlog_over_the_limit_aborts_the_session(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path, sync_backlog_limit=2)
    syncer._run = lambda *a, **kw: False  # type: ignore[method-assign]
    raws = [_write(syncer.config.output_dir / f"f{n}.cr2") for n in range(3)]

    syncer._ship(raws)

    assert syncer.fatal_error is not None
    assert "cannot keep up" in syncer.fatal_error
    # Nothing is thrown away under the default policy.
    assert all(raw.is_file() for raw in raws)
    assert syncer.pending() == 3


def test_backlog_drop_oldest_frees_ram_instead_of_aborting(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path, sync_backlog_limit=2, on_sync_backlog="drop-oldest")
    syncer._run = lambda *a, **kw: False  # type: ignore[method-assign]
    raws = [_write(syncer.config.output_dir / f"f{n}.cr2") for n in range(4)]

    syncer._ship(raws)

    assert syncer.fatal_error is None
    assert syncer.pending() == 1
    assert not raws[0].exists()
    assert raws[-1].is_file()


def test_failed_batch_is_retried_on_the_next_cycle(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path, sync_backlog_limit=10)
    calls: list[list[str]] = []

    def fake_run(argv, *, what):  # noqa: ANN001
        calls.append(argv)
        return False

    syncer._run = fake_run  # type: ignore[method-assign]
    first = _write(syncer.config.output_dir / "f0.cr2")
    second = _write(syncer.config.output_dir / "f1.cr2")

    syncer._ship([first])
    assert syncer.pending() == 1
    syncer._ship([second])

    assert [str(first), str(second)] == [a for a in calls[-1] if a.endswith(".cr2")]


# --------------------------------------------------------------------------
# Rotated log files
# --------------------------------------------------------------------------
def _rotated(syncer: RemoteSync, stamp: str, content: bytes) -> Path:
    path = syncer.config.output_dir / f"solramp_{stamp}.log"
    path.write_bytes(content)
    return path


@needs_rsync
def test_closed_log_files_are_deleted_once_shipped_and_active_one_kept(
    tmp_path: Path,
) -> None:
    syncer = _syncer(tmp_path)
    syncer.preflight()
    older = _rotated(syncer, "2024-06-21-18-00-00", b"older\n")
    old = _rotated(syncer, "2024-06-21-19-00-00", b"old\n")
    active = _rotated(syncer, "2024-06-21-20-00-00", b"active\n")

    syncer.push_sidecars()

    remote = Path(syncer.dest.remote_dir(syncer.session))
    assert not older.exists() and not old.exists()
    assert active.read_bytes() == b"active\n"
    for path, content in ((older, b"older\n"), (old, b"old\n"), (active, b"active\n")):
        assert (remote / path.name).read_bytes() == content


@needs_rsync
def test_active_log_is_appended_on_every_push(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path)
    syncer.preflight()
    active = _rotated(syncer, "2024-06-21-20-00-00", b"one\n")
    syncer.push_sidecars()
    with open(active, "ab") as handle:
        handle.write(b"two\n")

    syncer.push_sidecars()

    remote = Path(syncer.dest.remote_dir(syncer.session))
    assert (remote / active.name).read_bytes() == b"one\ntwo\n"


@needs_rsync
def test_legacy_single_log_is_shipped_and_removed_once_rotation_starts(
    tmp_path: Path,
) -> None:
    syncer = _syncer(tmp_path)
    syncer.preflight()
    legacy = _write(syncer.config.log_file_path, 64)
    active = _rotated(syncer, "2024-06-21-20-00-00", b"active\n")

    syncer.push_sidecars()

    remote = Path(syncer.dest.remote_dir(syncer.session))
    assert not legacy.exists()
    assert (remote / legacy.name).is_file()
    assert active.is_file()


def test_closed_log_files_are_kept_when_the_upload_fails(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path)
    syncer._run = lambda *a, **kw: False  # type: ignore[method-assign]
    old = _rotated(syncer, "2024-06-21-19-00-00", b"old\n")
    active = _rotated(syncer, "2024-06-21-20-00-00", b"active\n")

    syncer.push_sidecars()

    assert old.is_file() and active.is_file()


def test_rotated_log_upload_uses_append(tmp_path: Path) -> None:
    syncer = _syncer(tmp_path)
    calls: list[list[str]] = []

    def fake_run(argv, *, what):  # noqa: ANN001
        calls.append(argv)
        return True

    syncer._run = fake_run  # type: ignore[method-assign]
    _rotated(syncer, "2024-06-21-19-00-00", b"old\n")
    _rotated(syncer, "2024-06-21-20-00-00", b"active\n")

    syncer.push_sidecars()

    assert len(calls) == 2
    assert all("--append" in argv for argv in calls)


def test_worker_pushes_sidecars_while_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import solramp.sync as sync_module

    monkeypatch.setattr(sync_module, "_IDLE_SIDECAR_PUSH_SECONDS", 0.0)
    syncer = _syncer(tmp_path)
    pushed = threading.Event()
    syncer.push_sidecars = pushed.set  # type: ignore[method-assign]

    syncer.start()
    try:
        assert pushed.wait(timeout=5.0), "idle worker never pushed the side-cars"
    finally:
        syncer.stop()
        syncer.join(timeout=5.0)
