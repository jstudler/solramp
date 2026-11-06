# solramp — sun-driven exposure ramping for 24/7 timelapses

A day-to-night timelapse controller that drives a **Canon EOS 5D Mark IV** over
**PTP-IP (Wi-Fi/LAN) or USB** using the [`gphoto2`](https://pypi.org/project/gphoto2/)
Python bindings. It captures at a fixed interval and continuously *ramps* ISO,
aperture and shutter speed to hold an ETTR exposure as the light changes, while
keeping the per-frame metadata compatible with
[LRTimelapse](https://lrtimelapse.com/) deflicker/ramping. This is the technique
usually referred to as "holy grail" timelapse.

> The camera shoots raw CR2. The app **never modifies the CR2 or its EXIF** — it
> only reads copies on the computer to decide the next exposure.

---

## What it does

- **Smart-scheduled capture** at a fixed interval (drift-corrected). Each
  output file is named with the **actual shot-completion time** — the moment
  `time.time()` is sampled immediately after gphoto2 returns from the capture
  call — not the scheduled trigger time. Variations of a second or two relative
  to the nominal interval are therefore normal and reflect real exposure
  duration, camera buffer flush time, and USB/Wi-Fi handshake latency.
- **Exposure ramping**: adjusts ISO / aperture / shutter to
  keep highlights just below clipping (ETTR) and to transition smoothly from
  day to night and back.
- **CR2 analysis with rawpy**: each new frame is decoded to linear sRGB and
  measured for white/black clipping, mean & highlight EV and saturation.
- **YAML metadata side-car** updated after every frame (LRTimelapse-friendly),
  with **resume** support.
- **Three cooperating threads**: capture/scheduler, downloader, analyser.
- **Graceful shutdown** on SIGINT/SIGTERM (finishes the in-flight capture and
  downloads, disconnects the camera, then exits).
- Optional **`--auto-init`** that finds a safe starting exposure automatically.

---

## High-level design

```text
        ┌──────────────┐  capture path    ┌──────────────┐  cr2 + settings  ┌──────────────┐
        │  Scheduler   │ ───────────────▶ │  Downloader  │ ───────────────▶ │   Analyser   │
        │  (capture +  │   (queue)        │ (pull+delete │   (queue)        │ (rawpy +     │
        │  apply ramp) │ ◀─────────────── │  from camera)│                  │  ramping)    │
        └──────┬───────┘  pending ramp    └──────┬───────┘                  └──────┬───────┘
               │           (thread-safe slot)    │                                 │
               ▼                                  ▼                                 ▼
        gphoto2 (PTP-IP or USB) ◀── single camera lock ──▶ ./<session>/*.cr2    timelapse_metadata.yaml
```

- **Scheduler thread** — owns the capture cadence. Just after a capture (and at
  least 1 s before the next one) it applies any pending exposure change produced
  by the analyser, honouring the *2-same-frame* rule.
- **Downloader thread** — pulls the just-captured CR2 off the camera to disk and
  deletes it from the card, then hands the path + the settings used straight to
  the analyser (no filesystem race). A **watchdog** observer is used as a
  secondary safety net to pick up any `.cr2` that appears but was not enqueued
  (e.g. on resume).
- **Analyser thread** — decodes the CR2, computes metrics, asks the ramping
  engine for a decision, writes the metadata row, and posts any change back to
  the scheduler.

### The PTP lock model (important)

A single gphoto camera session **cannot run two transactions at once**. Issuing
`set_config` while a capture/download is in flight causes a PTP command
collision. Therefore **every** camera operation (capture, download, delete,
get/set config) is serialised through one re-entrant lock, `Camera.lock`. The
"parallelism" is that the downloader and analyser run while the scheduler is
*sleeping* between captures — not while the camera is mid-transaction.

Because of this, configuration changes are only applied in the safe window the
spec defines: **after** a capture completes and the frame has been handed to the
downloader, and **at least 1 s before** the next capture starts. See the capture
loop in `pipeline.py`.

> See [CAMERA_BEHAVIOUR.md](CAMERA_BEHAVIOUR.md) for the hardcoded behaviour and
> network quirks of *this* body; re-verify it with `uv run pytest -m e2e`.

---

## The ramping algorithm

Everything is computed in **brightness stops** (powers of two of light), so the
three axes combine linearly:

| axis | brightness contribution | brighter means |
|------|-------------------------|----------------|
| ISO       | `log2(iso / 100)`     | higher ISO  |
| shutter   | `log2(seconds)`       | longer time |
| aperture  | `-2 · log2(f_number)` | wider f-stop |

**1. Measure (rawpy).** Decode the CR2 with
`postprocess(no_auto_bright=True, use_camera_wb=True, gamma=(1,1), output_color=sRGB,
output_bps=16)`. `no_auto_bright=True` is essential — otherwise every frame is
normalised to the same brightness and the ramp signal disappears. From the
linear sRGB we compute:

- `clipping_white_pct` — pixels whose brightest channel ≥ 99 % of full scale.
- `clipping_black_pct` — pixels whose brightest channel ≤ ~1 % of full scale.
- `highlight_ev` — the 99.5th-percentile luminance in *stops below clipping*
  (the ETTR control signal).
- `mean_ev`, `mean_saturation` — logged for reference.

**2. Decide (priorities, in order):**

1. **Avoid clipping first (ETTR).** White clipping is worse than black, so it has
   a lower threshold and **forces** the image darker. Black clipping only nudges
   brighter and only when significant.
2. **Hit the target with margin.** We aim to keep the highlights ~1 stop below
   clipping (configurable `target_headroom_stops`). The extra stop is deliberate
   headroom so a sun entering the frame at sunset does not instantly blow out —
   we are **not allowed to ramp back and forth**.
3. A **dead-band** (default 1/3 stop) prevents reacting to noise and naturally
   helps satisfy the 2-same rule.

**3. Which axis moves** depends only on the direction of the change, and encodes
the spec's priorities (keep ISO low > shutter fast > aperture narrow):

- **Add light** (evening, scene darkening): **aperture → shutter → ISO**
  (open the aperture first, raise ISO only as a last resort).
- **Remove light** (morning, scene brightening): **ISO → shutter → aperture**
  (recover low ISO first, narrow the aperture last).

**4. Direction gating by sun phase.** Using the `astral` library we compute
whether the sun is descending (**darkening**) or ascending (**brightening**).
In the darkening phase the engine only *adds* light (except a white-clip
override); in the brightening phase it only *removes* light (except a black-clip
override). The boundary flips just twice a day (solar noon / solar midnight), so
the image ramps down then up — **at most one reversal per 24 h**.

**5. Anticipate fast transitions.** If three consecutive frames are all more than
1 EV off in the same direction, the scene is changing faster than a
1/3-stop-per-frame ramp can follow, so the engine **skips a step** (moves two
steps) on the single axis currently ramping.

**6. The 2-same rule.** LRTimelapse ramping compensation needs **at least two
consecutive frames with identical settings** after *any* change (including
skips). The scheduler enforces this: it will not apply a new change until at
least two frames have been shot at the current settings.

---

## Requirements

- macOS or Linux with [`uv`](https://docs.astral.sh/uv/) installed.
- A Canon EOS 5D Mark IV connected via gphoto2 over PTP-IP or USB and set to
  **Manual (M)**
  exposure mode.
- Python ≥ 3.12 (managed by `uv`).

The `gphoto2` and `rawpy` wheels bundle their native libraries, so no extra
system packages are required for the Python app itself.

### USB on macOS: release the camera from other apps

macOS and several photo apps claim the camera over PTP as soon as it is plugged
in, and gphoto2 then fails with `Could not claim the USB device`. Quit **Photos**,
**Image Capture**, and **EOS Utility** before starting a session, and check that
nothing else is holding the connection:

```bash
ps aux | grep -i -E 'ptpcamera|mscamerad|VDCAssistant|Image Capture|EOS Utility|Photos' | grep -v grep
```

If `PTPCamera`/`ptpcamerad` shows up, stop it (it respawns on the next plug-in):

```bash
killall PTPCamera 2>/dev/null; killall ptpcamerad 2>/dev/null
```

Then confirm the camera is free with `gphoto2 --auto-detect` and
`gphoto2 --summary`.

## Install

```bash
uv sync --extra dev
```

## Run

```bash
# Minimal: capture every 30 s into a timestamped folder, reading the camera's
# current exposure as the starting point.
uv run solramp --interval 30

# With a config file and an explicit output directory:
uv run solramp --config config.example.toml --interval 30 --output-dir ./tonight

# Let the app find a safe starting exposure automatically (ETTR with margin):
uv run solramp --interval 30 --auto-init
```

`solramp --help` documents every flag. Key ones:

| flag | meaning | default |
|------|---------|---------|
| `--interval` | seconds between captures (**required**, ≥ 20) | — |
| `--output-dir` | capture folder (**CLI-only**) | `./%Y-%m-%d-%H-%M-%S` |
| `--config` | path to a TOML config file | none |
| `--camera-transport` | camera connection transport: `ptpip` or `usb` | `ptpip` |
| `--camera-address` | camera IP/hostname (used with `ptpip`) | `192.168.111.222` |
| `--camera-selector` | USB camera selector (required when multiple USB cameras are present) | unset |
| `--auto-init` | auto-find a starting exposure | off |
| `--latitude/--longitude/--timezone` | location for sun times | Bern, CH |
| `--log-level` | stdout verbosity: `DEBUG`/`INFO`/`WARNING`/... | `INFO` |
| `--log-file/--no-log-file` | write a per-session log file into the capture folder | on |
| `--log-level-file` | log-file verbosity: `DEBUG`/`INFO`/`WARNING`/... | `DEBUG` |
| `--log-rotate` | start a new log file every `10m`/`1h`/`4h`/`1d`/..., aligned to the wall clock; `off` for a single `solramp.log` | `1h` |
| `--daemon` | run unattended: wait for the camera, one session per connection | off |
| `--camera-lost-cycles` | missed intervals before the session ends | `5` |
| `--daemon-poll-seconds` | how often to look for the camera while idle | `15` |
| `--visualizer/--no-visualizer` | serve the metadata dashboard while capturing | on |
| `--visualizer-only` | serve the dashboard and nothing else (no camera) | off |
| `--visualizer-host` / `--visualizer-port` | dashboard bind address and port | `127.0.0.1` / `8000` |
| `--additional-visualizer-folders` | extra folders to scan for sessions | none |

CLI values always override the TOML file, which overrides the built-in defaults.
The **output directory is CLI-only** and is never read from the TOML file.

### Resume

Point `--output-dir` at an existing session folder that already contains
`timelapse_metadata.yaml` and the app resumes: it logs the time gap since the
last analysed frame, picks up any not-yet-analysed CR2s, and continues.

### Daemon mode

```bash
uv run solramp --daemon --config config.example.toml --output-dir ~/Pictures/timelapse
```

The daemon polls for the camera configured by `camera_transport` /
`camera_address` (one ICMP ping for `ptpip`, one `gphoto2 --auto-detect` for
`usb`) and starts a session as soon as it answers. Each session runs in its own
timestamped folder under `--output-dir`, with its own log file and metadata
sidecar.

Short dropouts are absorbed by the normal capture-retry and PTP-reconnect path
and stay in the same session. Once `--camera-lost-cycles` intervals pass with no
successful capture (5 × the interval by default) the session ends, the daemon
goes back to waiting, and the **next connection starts a new folder**. A camera
that answers but is not in Manual mode is retried with a 30 s → 5 min backoff,
so it can be fixed in the field without touching the host.

`--output-dir` is required in daemon mode: the default folder is relative to the
working directory, which a service manager does not set predictably.

To start at boot, edit the paths in
[`packaging/net.solramp.plist`](packaging/net.solramp.plist) (macOS) or
[`packaging/solramp.service`](packaging/solramp.service) (Linux); each file
documents its own install commands. On macOS over USB, `ptpcamerad` claims Canon
bodies on plug-in — run `killall PTPCamera` (and disable Image Capture's
auto-launch) if gphoto2 reports "Could not claim the USB device".

### Stopping

Press `Ctrl-C` (SIGINT) or send SIGTERM. The app finishes the current capture
and any pending downloads, disconnects the camera, and exits cleanly. In daemon
mode this also stops the supervisor rather than starting another session.

### Remote sync (storage-less capture)

A Raspberry Pi with nothing but an SD card cannot absorb 33 MB CR2s at a 20 s
cadence — the card is both too slow and too easy to wear out. Instead, capture
into RAM and push every frame off the box as soon as it has been analysed:

```bash
sudo mount -t tmpfs -o size=1500M tmpfs /mnt/capture

uv run solramp --daemon --config config.toml \
  --output-dir /mnt/capture \
  --rsync-dest pi@nas.local:/srv/timelapse
```

`--rsync-dest` is **mandatory** when `--output-dir` is RAM-backed (`/dev/shm`,
`/run/shm`, or any `tmpfs`/`ramfs` mount): the app refuses to start a session it
has nowhere to drain. In that case `--delete-synced-raw` also defaults to on, so
each CR2 is unlinked the moment rsync confirms the transfer. A dedicated
size-capped tmpfs is preferable to `/dev/shm` in production — it gives a hard
bound on RAM use and no competition from other processes.

A CR2 is only needed between download and the end of its analysis (~0.7 s);
everything after that is filename-only. The metadata sidecar and the log file
are *also* uploaded after every frame — and at least once a minute while no
frames flow — so the remote copy is always current even if the Pi disappears
mid-sequence. A session that crashes still ships its traceback on the way out.

**Log rotation.** By default the session log is split into one file per hour,
aligned to the local wall clock (`solramp_2026-10-16-10-00-00.log` covers
10:00–11:00); `--log-rotate` picks another interval (minimum `1m`; a day starts
at local midnight) or `off`. The files are append-only and never renamed, so
each push ships only the new bytes (`rsync --append`). With `--rsync-dest`,
finished files are deleted locally once rsync confirms them; only the file
currently being written stays on the device. Without `--rsync-dest` every file
is kept.

**Why rsync-over-SSH and not an NFS mount.** On a 5G link that drops, every
failure mode has to be a return code you can test, not a blocked syscall you
cannot. An NFS write to a dead server either hangs the calling thread forever
(`hard`) or corrupts the file (`soft`); rsync just exits non-zero and the frame
is retried on the next cycle.

Sync runs on its own thread and never blocks the capture or the ramping path.
Transfers are resumable (`--partial-dir`), so a 33 MB upload killed at 90 %
picks up where it left off.

- **Authentication** uses the normal `~/.ssh` resolution (ssh_config, agent,
  default key names). `--rsync-ssh-key PATH` overrides it for an unattended
  host; the file must not be group- or world-readable. Key material is never
  accepted on the command line — it would leak through `ps`.
- **Host keys are checked as usual.** Seed the Pi once with
  `ssh-keyscan -H nas.local >> ~/.ssh/known_hosts`.
- **Startup is fail-fast**: before the camera is even touched, the app checks
  that `rsync` and `ssh` exist, creates the remote session folder, and round-trips
  a probe file. An unreachable destination exits with a diagnostic instead of
  filling RAM with frames.
- **Backpressure**: if un-uploaded frames pile past `--sync-backlog-limit`
  (20 frames ≈ 700 MB) or free space drops below three frames,
  `--on-sync-backlog` decides what happens — `abort` ends the session with every
  frame intact (the default; losing the tail beats losing the middle), or
  `drop-oldest` discards the oldest half and keeps shooting.
- `--rsync-bwlimit` (KiB/s) caps the upload on a metered link;
  `--rsync-timeout` bounds a stalled transfer.

### Visualizer

Every session writes a `timelapse_metadata.yaml` sidecar. The built-in dashboard
renders it: session metadata, a map of the capture location, and synchronised
charts of ISO / aperture / shutter and of clipping / EV / saturation, with the
day-night phase and every applied ramp marked on the timeline.

It runs automatically alongside a capture on <http://127.0.0.1:8000/> and polls
for changes, so the charts grow as the night progresses. To just look at past
sessions, skip the camera entirely:

```bash
uv run solramp --visualizer-only --output-dir ./captures
```

The watched folder is `--output-dir` **itself**, not the timestamped session
folder inside it, so every past run shows up in the dropdown next to the one
being shot; `--additional-visualizer-folders DIR [DIR ...]` adds more roots.
Without `--output-dir` the current working directory is watched.

Use `--no-visualizer` to turn it off, or `--visualizer-port 0` to pick any free
port (the bound URL is logged). The dashboard has **no authentication** and
exposes capture paths and GPS coordinates, so it binds to loopback only —
`--visualizer-host 0.0.0.0` publishes it to the whole network and logs a warning.

## Running in a container

[`Dockerfile`](Dockerfile) builds a `linux/amd64` + `linux/arm64` image carrying
the app and the external binaries it shells out to: `gphoto2` (USB
auto-detect), `rsync` and `ssh` (remote sync), and `ping` (PTP-IP reachability).

```bash
# One architecture, loaded into the local image store:
docker buildx build --platform linux/arm64 -t solramp:dev-arm64 --load .

# Both architectures as a real manifest list:
podman build --platform linux/amd64,linux/arm64 --manifest solramp:dev .
```

**macOS cannot run this over USB.** Docker Desktop, Colima, OrbStack and podman
machine are all Linux VMs with no USB passthrough. On a Mac, either run natively
with `uv run` or use the `ptpip` transport over WiFi.

### USB

```bash
podman run -d --restart=always --name solramp \
  --group-add keep-groups \
  --shm-size=2g \
  -p 8000:8000 \
  -e TZ="$(cat /etc/timezone)" \
  -v /dev/bus/usb:/dev/bus/usb \
  -v "$PWD/config.example.toml:/config.toml:ro" \
  -v "$HOME/.ssh/id_ed25519:/root/.ssh/id_ed25519:ro" \
  -v "$HOME/.ssh/known_hosts:/root/.ssh/known_hosts:ro" \
  solramp:dev \
    --config /config.toml \
    --camera-transport usb \
    --interval 40 \
    --auto-init \
    --output-dir /dev/shm/ \
    --rsync-dest user@192.168.111.111:/var/lib/solramp \
    --log-level-file DEBUG \
    --target-headroom-stops 1.5 \
    --target-headroom-stops-night 0.5 \
    --deadband-stops 0.2 \
    --white-clip-pct-threshold 0.03 \
    --visualizer \
    --visualizer-host=0.0.0.0 \
    --daemon
```

Both halves are needed. The bind mount supplies the device *path*; the cgroup
rule (major 189 = `usb_device`) supplies *permission* — without it the node is
visible but every open returns `EPERM`. Mount the whole `/dev/bus/usb`
directory rather than a single `--device`: libgphoto2 re-enumerates and the
camera takes a new bus/device number after each power-cycle, so a static device
path goes stale the first time the camera sleeps.

On a desktop Linux host, mask `gvfs-gphoto2-volume-monitor` first or it claims
the camera before the container can.

Without `TZ` the container runs in UTC, so the session folder and every frame
filename are stamped hours away from the wall clock the sequence was shot
against. `/etc/timezone` is the host's IANA name on Debian/Raspberry Pi OS; pass
the name explicitly (`-e TZ=Europe/Zurich`) elsewhere. Bind-mounting
`/etc/localtime` works on a Linux host but not on macOS, where the mount comes
from the container VM rather than the Mac.

### PTP-IP

Nothing device-specific is required — no mounts, no cgroup rules, no privileges.
Only one PTP-IP session exists at a time, so never run two containers against
one camera.

### compose

[`compose.yaml`](compose.yaml) wires both variants up:

```bash
docker compose up            # USB
docker compose up ptpip      # network
```

Worth knowing:

- `shm_size: 2gb` — the Pi recipe writes frames to `/dev/shm`, and the default
  64 MB holds two CR2s.
- `TZ` — defaults to `Europe/Zurich`; override it with `TZ=... docker compose up`
  or the image stamps folders and filenames in UTC.
- `stop_signal: SIGINT` with a 180 s grace period, so a long exposure plus
  in-camera long-exposure NR can finish instead of being killed mid-frame.
- `--visualizer-host=0.0.0.0` is required inside a container (the default
  loopback bind is unreachable from the host), so the published port is pinned
  to `127.0.0.1` on the host side.
- The ssh key is bind-mounted read-only and must be `chmod 600`; ssh runs in
  `BatchMode`, so the remote host must already be in the mounted `known_hosts`.

### What the image pins

Both base images are pinned by digest, the apt packages by exact version
(verified identical on both architectures), and every wheel by the sha256
hashes already in `uv.lock`, which `uv sync --frozen` enforces. The app is
installed non-editable and byte-compiled, so the runtime stage needs nothing
from the build context.

The image deliberately carries **two** copies of libgphoto2: 2.5.28 from
Debian, behind the `gphoto2` CLI that `UsbTransport` shells out to, and 2.5.33
vendored inside the `gphoto2` wheel that the app itself links against. Removing
the apt package to "deduplicate" silently breaks USB auto-detect.

`ping` has its `cap_net_raw` file capability stripped at build time. Left in
place, runtimes that drop `NET_RAW` from the bounding set (podman's default)
refuse to even `exec` it, which would make `is_reachable()` report every camera
as unreachable; without the file capability it falls back to unprivileged ICMP
sockets and works under both docker and podman.

## Configuration

See [`config.example.toml`](config.example.toml) for every option with comments.
The output directory cannot be set there (CLI only).

## Testing & development

```bash
# Unit tests + (skips camera e2e tests automatically):
uv run pytest

# Include the hardware end-to-end tests (camera must be reachable):
uv run pytest -m e2e

# Benchmarks for the local image-analysis / ramping functions:
uv run pytest benchmarks/ --benchmark-only --benchmark-min-rounds=20
```

- **Unit tests** mock the camera and feed **synthetic numpy arrays** to the
  analysis/ramping code, plus a couple of raw frames generated on the fly by
  `tests/synth_raw.py` (shared with the benchmarks).
- **e2e tests** talk to the real camera (connect, capture, download, change a
  setting, read status). They **fail if the camera is unreachable** and are
  excluded from the default `pytest` run.
- **Benchmarks** time the pure-CPU functions (raw analysis, ramping decisions)
  and reuse the same generator at full sensor resolution.

> No raw files are committed. `tests/synth_raw.py` builds tiny uncompressed
> DNGs (~12 kB) that libraw really decodes, so the analyser is still tested
> end-to-end — at known pixel levels, which lets the tests assert exact metered
> EV and clipping values. Since the fixtures are uncompressed, the decode
> *benchmarks* run faster than a real CR2 would; see the note in
> `benchmarks/test_bench_analysis.py`.

---

## Hardcoded for the Canon 5D Mark IV

To make this app generic, change the following (all clearly localised):

- **`camera_tables.py`** - `ISO_LABELS` and `SHUTTER_LABELS` are still the 5D
  Mark IV's exact PTP choice lists. `APERTURE_LABELS` is now a fallback table;
  the active aperture list is discovered from `/main/capturesettings/aperture`
  at runtime for the mounted lens.
- **gphoto2 config paths** in `camera.py`:
  - ISO: `/main/imgsettings/iso`
  - aperture: `/main/capturesettings/aperture`
  - shutter: `/main/capturesettings/shutterspeed`
  - exposure mode check: `/main/capturesettings/autoexposuremode`
  These paths are Canon-EOS specific.
- **Manual-mode detection** — we require `autoexposuremode == "Manual"`; the exact
  label is Canon-specific.
- **CR2 white level** — the analyser assumes 16-bit postprocess output saturates
  at 65535; fine for any rawpy-supported camera, but the file extension `.cr2`
  and the `%C` extension handling assume Canon raw.
- **Max shutter 30 s** — the 5D Mark IV's longest non-bulb exposure.
- **Transfer time / min interval (20 s)** — sized for the ~33 MB CR2 over this
  body's Wi-Fi; other bodies/links differ.
- **Default camera address** `192.168.111.222`.

---

## Future enhancement: live metering from the camera

This app analyses downloaded CR2s, which arrive a few seconds late. Canon EOS
bodies can expose a **live histogram / exposure level** over PTP that would let
us ramp with near-zero latency and without decoding a raw:

- `gphoto2` live-view (`camera.capture_preview()`) returns a JPEG whose histogram
  could be metered directly.
- The Canon PTP property **`/main/capturesettings/exposurelevel`** (light meter
  indicator) and the EOS event stream can report metering deltas live.

Using either would remove the download-and-decode latency and is a natural next
step. It is **not** implemented here because the spec asked for rawpy-based CR2
analysis.

## Licence

Copyright (C) 2026 Joel Studler.

solramp is free software: you may redistribute it and/or modify it under the
terms of the **GNU Affero General Public License, version 3 or later** — see
[`LICENSE`](LICENSE).

In short: reuse it, sell it, build on it — but if you distribute a modified
version, **or let other people use one over a network** (the built-in
visualizer dashboard counts), you must offer those users the complete
corresponding source under the same licence.

Contributions are accepted under the same licence (inbound = outbound). There is
no CLA and no separate commercial licence.

### Third-party licences

Dependencies are installed from PyPI and keep their own licences: `gphoto2`
(python-gphoto2) is **LGPL-3.0-or-later**, `rawpy` is MIT (wrapping LibRaw,
LGPL-2.1/CDDL), `astral` and `watchdog` are Apache-2.0, `numpy` is BSD-3-Clause
and `pyyaml` is MIT. A plain `pip`/`uv` install raises no extra obligations; if
you ever ship a bundled binary or container image that *includes* libgphoto2,
honour the LGPL's relinking requirement.
