# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stream captures off the device with rsync-over-SSH.

Designed for a storage-less field unit (Raspberry Pi writing to ``/dev/shm``):
every analysed CR2 is pushed to a remote host and the local copy is dropped as
soon as the receiver has confirmed it, so the working set stays bounded by the
upload rate rather than by the card.

Two things make this safe on a link that drops (5G, marginal wifi):

* ``--remove-source-files`` lets *rsync* do the deleting - it removes a source
  file only after the receiver confirms that file, so a partially shipped batch
  leaves exactly the un-shipped files behind and the next cycle retries them.
* every failure surfaces as a **process exit code**, never as a blocked syscall,
  so the sync thread can always be joined and the backlog is always observable.

The metadata side-car and the active log file are *not* removed locally (resume
needs the metadata); they are re-pushed after every batch - and at least every
minute while no frames flow - so a power cut costs at most one frame. Finished
(rotated) log files are deleted locally once rsync has confirmed them.
"""

from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .logging_setup import get_logger, split_log_files

if TYPE_CHECKING:
    from .config import Config

_log = get_logger("sync")

# Rough size of one 5D Mark IV CR2; used for free-space budgeting only.
APPROX_FRAME_BYTES = 35 * 1024 * 1024
# rsync exit code 24 = "partial transfer due to vanished source files".
_RSYNC_VANISHED = 24
_PARTIAL_DIR = ".rsync-partial"
_PROBE_NAME = ".solramp-probe"
_MAX_BATCH = 16
_SSH_CONNECT_TIMEOUT = 10
_PREFLIGHT_TIMEOUT = 20.0
# Ship the side-cars at least this often even when no frames are flowing, so a
# hung camera or a stuck session still gets its log tail off the device.
_IDLE_SIDECAR_PUSH_SECONDS = 60.0

# [user@]host:/path, with optional [bracketed] IPv6 literal.
_REMOTE_SPEC = re.compile(
    r"""^
    (?:(?P<user>[^@/\[\]:]+)@)?
    (?:\[(?P<host6>[^\]]+)\]|(?P<host>[^:/\[\]]+))
    :(?P<path>.*)
    $""",
    re.VERBOSE,
)


class SyncError(Exception):
    """Raised for an invalid destination or an unusable remote."""


@dataclass(frozen=True)
class RsyncDest:
    """A parsed rsync destination: a remote ``[user@]host:/base`` or a local dir."""

    path: str
    host: str | None = None
    user: str | None = None

    @classmethod
    def parse(cls, spec: str) -> "RsyncDest":
        spec = (spec or "").strip()
        if not spec:
            raise SyncError("rsync destination is empty")
        if "://" in spec:
            raise SyncError(
                f"rsync destination {spec!r} uses a URL scheme; only the "
                "'[user@]host:/path' (ssh) form and local directories are supported"
            )

        match = _REMOTE_SPEC.match(spec)
        if match is None:
            local = Path(spec).expanduser()
            if not local.is_dir():
                raise SyncError(
                    f"rsync destination {spec!r} is neither '[user@]host:/path' nor "
                    "an existing local directory"
                )
            return cls(path=str(local))

        path = match.group("path")
        if path.startswith(":"):
            raise SyncError(
                f"rsync destination {spec!r} uses rsync-daemon syntax (host::module); "
                "use the ssh form 'host:/path'"
            )
        if path.startswith("~"):
            # --secluded-args stops the remote shell expanding '~', so it would
            # be taken literally and silently create a directory called "~".
            raise SyncError(
                f"rsync destination {spec!r} must use an absolute remote path, not '~'"
            )
        return cls(
            path=path or ".",
            host=match.group("host6") or match.group("host"),
            user=match.group("user"),
        )

    @property
    def is_local(self) -> bool:
        return self.host is None

    @property
    def ssh_target(self) -> str:
        """``[user@]host`` for a plain ``ssh`` invocation."""
        assert self.host is not None
        return f"{self.user}@{self.host}" if self.user else self.host

    def remote_dir(self, session: str) -> str:
        """Destination directory for one session, as the receiver sees it."""
        base = self.path.rstrip("/")
        if self.is_local:
            return str(Path(base or "/") / session)
        return f"{base}/{session}" if base not in ("", ".") else session

    def target(self, session: str) -> str:
        """Destination argument handed to rsync (trailing slash = 'into this dir')."""
        directory = self.remote_dir(session)
        if self.is_local:
            return f"{directory}/"
        host = f"[{self.host}]" if ":" in (self.host or "") else self.host
        prefix = f"{self.user}@{host}" if self.user else host
        return f"{prefix}:{directory}/"

    def describe(self) -> str:
        return self.path if self.is_local else f"{self.ssh_target}:{self.path}"


def ssh_command(
    *,
    key: Path | None = None,
    port: int | None = None,
    control_path: Path | None = None,
) -> list[str]:
    """Build the ``ssh`` argv used both directly and as rsync's ``-e``.

    ``BatchMode=yes`` is non-negotiable: a headless unit must fail fast rather
    than block forever on a passphrase prompt. Host-key checking is deliberately
    left at the ssh default - never disabled.
    """
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={_SSH_CONNECT_TIMEOUT}",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
    ]
    if control_path is not None:
        # One handshake per session; later rsync calls multiplex over it.
        cmd += [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control_path}",
            "-o", "ControlPersist=10m",
        ]
    if key is not None:
        cmd += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    if port is not None:
        cmd += ["-p", str(port)]
    return cmd


def build_rsync_argv(
    sources: Sequence[Path | str],
    target: str,
    *,
    ssh: str | None = None,
    remove_source_files: bool = False,
    append: bool = False,
    bwlimit: int | None = None,
    timeout: float = 120.0,
) -> list[str]:
    """Assemble one rsync call.

    ``-rt`` rather than ``-a``: ``-a`` implies ``-og``, which errors on a
    receiver that is not root. No ``-z`` either - CR2s are already compressed,
    so it only burns Pi CPU.
    """
    argv = ["rsync", "-rt", "-s", f"--timeout={max(1, int(timeout))}"]
    if append:
        # --append implies --inplace, which conflicts with --partial-dir.
        argv += ["--append", "--inplace"]
    else:
        argv.append(f"--partial-dir={_PARTIAL_DIR}")
    if remove_source_files:
        argv.append("--remove-source-files")
    if bwlimit:
        argv.append(f"--bwlimit={bwlimit}")
    if ssh:
        argv += ["-e", ssh]
    argv += [str(source) for source in sources]
    argv.append(target)
    return argv


def _tail(text: str | None, lines: int = 3) -> str:
    return " | ".join((text or "").strip().splitlines()[-lines:])


def _control_dir() -> Path:
    """A short-lived 0700 directory for the ssh ControlPath socket.

    Kept short on purpose: the socket path must fit in sockaddr_un (~104 bytes).
    """
    run_user = Path(f"/run/user/{os.getuid()}")
    base = run_user if run_user.is_dir() and os.access(run_user, os.W_OK) else Path(
        tempfile.gettempdir()
    )
    directory = base / f"hg-{os.getpid()}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def validate_ssh_key(path: Path) -> Path:
    """Check an ssh key path before ssh rejects it with a cryptic error."""
    key = path.expanduser()
    if not key.is_file():
        raise SyncError(f"rsync ssh key {key} does not exist")
    mode = stat.S_IMODE(key.stat().st_mode)
    if mode & 0o077:
        raise SyncError(
            f"rsync ssh key {key} is group/world accessible (mode {mode:04o}); "
            f"ssh will refuse it - run: chmod 600 {key}"
        )
    return key


class RemoteSync:
    """Owns the sync thread: ships analysed frames, then frees them locally."""

    def __init__(self, config: "Config", dest: RsyncDest) -> None:
        self.config = config
        self.dest = dest
        self.session = config.output_dir.name
        self.target = dest.target(self.session)
        self.fatal_error: str | None = None

        self._queue: "queue.Queue[Path]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backlog: list[Path] = []
        self._control_dir: Path | None = None
        # The worker and the final shutdown push may overlap if join() times out.
        self._sidecar_lock = threading.Lock()
        self._last_sidecar_push = time.monotonic()
        self._ssh_key = (
            validate_ssh_key(config.rsync_ssh_key)
            if config.rsync_ssh_key is not None
            else None
        )

    @classmethod
    def from_config(cls, config: "Config") -> "RemoteSync | None":
        """Return a syncer, or ``None`` when no destination is configured."""
        if not config.rsync_dest:
            return None
        return cls(config, RsyncDest.parse(config.rsync_dest))

    # -- command construction -------------------------------------------
    def _ssh_argv(self) -> list[str]:
        return ssh_command(
            key=self._ssh_key,
            port=self.config.rsync_ssh_port,
            control_path=self._control_path(),
        )

    def _control_path(self) -> Path | None:
        if self.dest.is_local:
            return None
        if self._control_dir is None:
            self._control_dir = _control_dir()
        return self._control_dir / "cm"

    def _rsync_ssh(self) -> str | None:
        return None if self.dest.is_local else shlex.join(self._ssh_argv())

    def _rsync(
        self,
        sources: Sequence[Path | str],
        *,
        timeout: float | None = None,
        target: str | None = None,
        **kwargs,
    ) -> list[str]:
        return build_rsync_argv(
            sources,
            self.target if target is None else target,
            ssh=self._rsync_ssh(),
            bwlimit=self.config.rsync_bwlimit,
            timeout=self.config.rsync_timeout if timeout is None else timeout,
            **kwargs,
        )

    # -- startup --------------------------------------------------------
    def preflight(self) -> None:
        """Fail fast if the destination is not usable. Raises :class:`SyncError`."""
        if shutil.which("rsync") is None:
            raise SyncError("rsync is not installed on this device")

        if self.dest.is_local:
            directory = Path(self.dest.remote_dir(self.session))
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SyncError(f"cannot create {directory}: {exc}") from exc
        else:
            if shutil.which("ssh") is None:
                raise SyncError("ssh is not installed on this device")
            self._remote_mkdir()

        self._probe_round_trip()
        self._log_budget()
        _log.info("Remote sync ready: %s", self.target)

    def _remote_mkdir(self) -> None:
        remote = self.dest.remote_dir(self.session)
        argv = self._ssh_argv() + [
            self.dest.ssh_target,
            f"mkdir -p -- {shlex.quote(remote)}",
        ]
        result = self._exec(argv, timeout=_PREFLIGHT_TIMEOUT)
        if result is None or result.returncode != 0:
            raise SyncError(self._ssh_failure_hint(result, remote))

    def _ssh_failure_hint(
        self, result: "subprocess.CompletedProcess[str] | None", remote: str
    ) -> str:
        stderr = _tail(result.stderr if result else None)
        base = (
            f"cannot reach rsync destination {self.dest.describe()} "
            f"(creating {remote} failed)"
        )
        if "Host key verification failed" in stderr:
            return (
                f"{base}: unknown or changed host key. Verify the fingerprint, then: "
                f"ssh-keyscan -H {self.dest.host} >> ~/.ssh/known_hosts"
            )
        if "Permission denied" in stderr:
            return (
                f"{base}: ssh authentication failed. Install a passphrase-less key "
                "for this device (BatchMode cannot prompt), or pass --rsync-ssh-key."
            )
        return f"{base}: {stderr or 'no output'}"

    def _probe_round_trip(self) -> None:
        """Upload and delete a 1-byte file.

        The only reliable way to catch a missing ``rsync`` on the remote or a
        read-only destination before the first frame is captured.
        """
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / _PROBE_NAME
            probe.write_text("x", encoding="utf-8")
            argv = self._rsync([probe], timeout=_PREFLIGHT_TIMEOUT)
            result = self._exec(argv, timeout=_PREFLIGHT_TIMEOUT)
        if result is None or result.returncode != 0:
            stderr = _tail(result.stderr if result else None)
            hint = ""
            if "command not found" in stderr or "No such file" in stderr:
                hint = " (is rsync installed on the remote?)"
            raise SyncError(
                f"test upload to {self.target} failed{hint}: {stderr or 'no output'}"
            )
        self._remove_remote_probe()

    def _remove_remote_probe(self) -> None:
        remote = f"{self.dest.remote_dir(self.session)}/{_PROBE_NAME}"
        if self.dest.is_local:
            Path(remote).unlink(missing_ok=True)
            return
        self._exec(
            self._ssh_argv() + [self.dest.ssh_target, f"rm -f -- {shlex.quote(remote)}"],
            timeout=_PREFLIGHT_TIMEOUT,
        )

    def _log_budget(self) -> None:
        free = free_bytes(self.config.output_dir)
        if free is None:
            return
        frames = int(free // APPROX_FRAME_BYTES)
        needed_mbit = APPROX_FRAME_BYTES * 8 / self.config.interval_seconds / 1e6
        _log.info(
            "Capture buffer: %.0f MB free in %s (~%d frames); sustaining %gs "
            "intervals needs ~%.1f Mbit/s uplink",
            free / 1e6,
            self.config.output_dir,
            frames,
            self.config.interval_seconds,
            needed_mbit,
        )
        if frames < self.config.sync_backlog_limit:
            _log.warning(
                "Free space holds only ~%d frames, below the backlog limit of %d; "
                "consider a larger tmpfs or --sync-backlog-limit",
                frames,
                self.config.sync_backlog_limit,
            )

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        """Start the worker. Idempotent: auto-init starts it before the pipeline."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="sync", daemon=True)
        self._thread.start()

    def enqueue(self, path: Path) -> None:
        self._queue.put(path)

    def drain(self) -> None:
        """Block until every queued file has had its upload attempt."""
        if self._thread is not None and self._thread.is_alive():
            self._queue.join()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 120.0) -> None:
        """Drain the queue and wait for the worker to finish its last batch."""
        self.drain()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def close(self) -> None:
        """Tear down the shared ssh connection. Safe to call more than once."""
        if self._control_dir is None:
            return
        self._exec(
            self._ssh_argv() + ["-O", "exit", self.dest.ssh_target], timeout=10.0
        )
        shutil.rmtree(self._control_dir, ignore_errors=True)
        self._control_dir = None

    def pending(self) -> int:
        return self._queue.qsize() + len(self._backlog)

    # -- worker ---------------------------------------------------------
    def _loop(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    return
                if (
                    time.monotonic() - self._last_sidecar_push
                    >= _IDLE_SIDECAR_PUSH_SECONDS
                ):
                    self._idle_push()
                continue
            batch = [first]
            while len(batch) < _MAX_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._ship(batch)
            except Exception as exc:  # noqa: BLE001 - the thread must survive
                _log.exception("Sync batch failed: %s", exc)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _ship(self, paths: list[Path]) -> None:
        candidates = [p for p in self._backlog + paths if p.exists()]
        self._backlog = []
        if not candidates:
            self.push_sidecars()
            return

        started = time.perf_counter()
        failed: list[Path] = []
        for target, group in self._group_by_target(candidates):
            if not self._run(
                self._rsync(
                    group,
                    target=target,
                    remove_source_files=self.config.delete_synced_raw,
                ),
                what=f"rsync of {len(group)} raw file(s)",
            ):
                failed += group
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if self.config.delete_synced_raw:
            # rsync removes each source only once the receiver confirms it, so
            # whatever still exists is exactly what has to be retried.
            self._backlog = [p for p in candidates if p.exists()]
        else:
            self._backlog = [p for p in failed if p.exists()]

        held = set(self._backlog)
        shipped = [p for p in candidates if p not in held]
        if shipped:
            _log.info(
                "Synced frame(s) %s in %.0fms",
                ", ".join(self._display_name(p) for p in shipped),
                elapsed_ms,
            )
        if self._backlog:
            _log.warning(
                "%d frame(s) still queued for upload (%.0f MB held locally)",
                len(self._backlog),
                sum(p.stat().st_size for p in self._backlog if p.exists()) / 1e6,
            )
        self.push_sidecars()
        self._check_backlog()

    def _relative_dir(self, path: Path) -> str:
        """``path``'s directory relative to the session dir ("" at top level)."""
        try:
            rel = path.parent.relative_to(self.config.output_dir)
        except ValueError:
            return ""
        return "" if rel == Path(".") else rel.as_posix()

    def _display_name(self, path: Path) -> str:
        sub = self._relative_dir(path)
        return f"{sub}/{path.name}" if sub else path.name

    def _group_by_target(self, paths: list[Path]) -> list[tuple[str, list[Path]]]:
        """Split a batch so sub-directory files (e.g. ``.autoinit/``) land in
        the same sub-directory remotely; rsync creates that last level itself."""
        groups: dict[str, list[Path]] = {}
        for path in paths:
            groups.setdefault(self._relative_dir(path), []).append(path)
        return [
            (f"{self.target}{sub}/" if sub else self.target, group)
            for sub, group in groups.items()
        ]

    def _idle_push(self) -> None:
        try:
            self.push_sidecars()
        except Exception as exc:  # noqa: BLE001 - the thread must survive
            _log.exception("Idle side-car push failed: %s", exc)

    def push_sidecars(self) -> None:
        """Re-send the metadata YAML and the logs.

        The metadata and the active log file are never removed locally. Finished
        rotated log files are deleted once rsync has confirmed them.
        """
        with self._sidecar_lock:
            self._last_sidecar_push = time.monotonic()
            metadata = self.config.metadata_path
            if metadata.is_file():
                self._run(self._rsync([metadata]), what="rsync of metadata")
            self._push_logs()

    def _push_logs(self) -> None:
        base = self.config.log_file_path
        if base is None:
            return
        closed, active = split_log_files(
            base, rotating=self.config.log_rotate_seconds is not None
        )
        # Log files are strictly append-only and never renamed, so --append
        # ships only the new tail bytes and never rewrites the remote copy.
        if closed and self._run(
            self._rsync(closed, append=True), what=f"rsync of {len(closed)} log file(s)"
        ):
            for path in closed:
                path.unlink(missing_ok=True)
            _log.debug("Removed shipped log file(s) %s",
                       ", ".join(path.name for path in closed))
        if active is not None:
            self._run(self._rsync([active], append=True), what="rsync of log")

    def _check_backlog(self) -> None:
        if self.fatal_error is not None:
            return
        limit = self.config.sync_backlog_limit
        free = free_bytes(self.config.output_dir)
        starved = free is not None and free < 3 * APPROX_FRAME_BYTES
        if len(self._backlog) < limit and not starved:
            return

        reason = (
            f"only {free / 1e6:.0f} MB free in {self.config.output_dir}"
            if starved and free is not None
            else f"{len(self._backlog)} frames queued (limit {limit})"
        )
        if self.config.on_sync_backlog == "drop-oldest":
            self._drop_oldest(reason)
            return
        self.fatal_error = (
            f"upload cannot keep up: {reason}. The capture directory is RAM-backed, "
            "so the session is stopping rather than silently losing frames. Increase "
            "the interval, lower the resolution of the link, or use "
            "--on-sync-backlog drop-oldest."
        )
        _log.error("%s", self.fatal_error)

    def _drop_oldest(self, reason: str) -> None:
        keep = max(1, self.config.sync_backlog_limit // 2)
        doomed = sorted(self._backlog)[: max(0, len(self._backlog) - keep)]
        for path in doomed:
            path.unlink(missing_ok=True)
        self._backlog = [p for p in self._backlog if p not in set(doomed)]
        if doomed:
            _log.error(
                "Dropped %d un-synced frame(s) to free RAM (%s); the sequence will "
                "have a gap", len(doomed), reason,
            )

    # -- subprocess helpers ---------------------------------------------
    def _run(self, argv: list[str], *, what: str) -> bool:
        result = self._exec(argv, timeout=self.config.rsync_timeout + 30.0)
        if result is None:
            return False
        if result.returncode == 0:
            return True
        if result.returncode == _RSYNC_VANISHED:
            _log.debug("%s: source files vanished mid-transfer; treating as done", what)
            return True
        _log.warning(
            "%s failed (exit %d): %s", what, result.returncode, _tail(result.stderr)
        )
        return False

    def _exec(
        self, argv: list[str], *, timeout: float
    ) -> "subprocess.CompletedProcess[str] | None":
        _log.debug("exec: %s", shlex.join(argv))
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            _log.warning("Command timed out after %.0fs: %s", timeout, argv[0])
        except OSError as exc:
            _log.warning("Command could not be started (%s): %s", argv[0], exc)
        return None


def free_bytes(path: Path) -> int | None:
    """Free bytes on the filesystem holding ``path`` (nearest existing parent)."""
    for candidate in [path, *path.parents]:
        try:
            usage = os.statvfs(candidate)
        except OSError:
            continue
        return usage.f_bavail * usage.f_frsize
    return None
