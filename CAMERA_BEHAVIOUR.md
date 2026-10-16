# Canon EOS 5D Mark IV — camera behaviour & hardcoded assumptions

This file documents the **camera-specific** behaviour the app relies on. Every
assumption here is isolated in code so a different body can be supported by
changing one module (see `src/holygrail/camera_tables.py` and the constants at
the top of `src/holygrail/camera.py`).

Body used during development: **Canon EOS 5D Mark IV**.

---

## Connection (PTP-IP)

* The camera advertises `_ptp._tcp.local` (as `ICPO-WFTEOSSystemService<id>`,
  where `<id>` is derived from the body's wireless adapter) on
  **TCP port 15740**.
* gphoto2 addresses it with the port scheme **`ptpip:<ip-or-host>`**, e.g.
  `ptpip:192.168.111.222`.
* **The model is not auto-detected.** Over PTP/IP `Camera.init()` will fail with
  `[-105] Unknown model` unless the abilities are set explicitly first. The body
  reports itself as the generic libgphoto2 model **`"PTP/IP Camera"`**
  (`PTPIP_MODEL`). `Camera._prepare_camera()` therefore does, in order:
  1. load a `PortInfoList`, `lookup_path("ptpip:<ip>")`, `camera.set_port_info(...)`;
  2. load a `CameraAbilitiesList`, `lookup_model("PTP/IP Camera")`,
     `camera.set_abilities(...)`;
  3. `camera.init()`.
* **List lifetime / segfault hazard:** the `PortInfo` and `CameraAbilities`
  objects point *into* their parent list's C memory. The `PortInfoList` and
  `CameraAbilitiesList` must outlive the `Camera`, or libgphoto2 reads freed
  memory and the process segfaults inside `init()`. The app keeps both lists on
  the instance (`self._port_info_list`, `self._abilities_list`) for the whole
  connection and clears them in `disconnect()`.
* **Single PTP-IP session only.** The WFT accepts exactly **one** TCP session at
  a time on port 15740. Anything that completes a TCP handshake to that port —
  including a bare `socket.create_connection()` probe — consumes the single slot
  and the camera then refuses the real `init()` for several seconds. For this
  reason reachability is checked with an **ICMP ping**, never a TCP connect
  (`Camera.is_reachable()` runs `ping -c 1 <addr>`). Ping verifies network
  presence without touching the PTP port, so it never competes with the session.
  `wait_until_reachable()` polls the ping before connecting.

### Pairing GUID quirk (important)

The first time gphoto2 talks to the camera it sends **its own GUID** during the
PTP/IP Init handshake, and the camera commits to it
(`~/.gphoto/settings` → `ptp2_ip=guid=…`). After that:

* EOS Utility (a *different* GUID) will silently stop listing the camera until
  you reset its Wi-Fi session on-body (MENU → Wi-Fi function → disconnect, or
  *Clear network settings*).
* Conversely, once paired to gphoto2 the camera reconnects to **this** app
  without re-pairing. Keep the gphoto2 GUID stable.

If `gphoto2 --port ptpip:<ip> --summary` works, the app will connect.

---

## Required camera state

| Requirement | Why | Enforced by |
|---|---|---|
| **Manual (M) exposure mode** | The app sets ISO/aperture/shutter explicitly; any auto mode would fight the ramp. | `Camera.is_manual_mode()`; startup aborts otherwise. |
| RAW (CR2) capture | Exposure analysis decodes CR2 with rawpy. | n/a (shoot RAW; CR2 magic verified in e2e). |
| Lens with electronic aperture | Aperture is ramped over PTP. | n/a |
| Charged battery / AC | Night exposures up to 30 s + continuous shooting. | n/a |

The app reads `autoexposuremode` and refuses to start unless it equals
`"Manual"`.

---

## gphoto2 config leaves used

All are single-config leaves under `/main/...` (confirmed via `list-config`):

| Logical axis | gphoto2 leaf (`get_single_config` name) | Constant |
|---|---|---|
| ISO | `iso` (`/main/imgsettings/iso`) | `ISO_CONFIG` |
| Aperture | `aperture` (`/main/capturesettings/aperture`) | `APERTURE_CONFIG` |
| Shutter | `shutterspeed` (`/main/capturesettings/shutterspeed`) | `SHUTTER_CONFIG` |
| Exposure mode | `autoexposuremode` (`/main/capturesettings/autoexposuremode`) | `AUTOEXPOSUREMODE_CONFIG` |
| Capture target | `capturetarget` (`/main/settings/capturetarget`) | `CAPTURETARGET_CONFIG` |

On connect the app sets `capturetarget` to **"Internal RAM"** (`CAPTURE_TARGET_RAM`).
Counter-intuitively, over PTP/IP this body fails `gp_camera_capture()` with
**`[-1] Unspecified error`** when the target is the memory card, but succeeds
when the frame is buffered in the camera's internal RAM and pulled from there
(capture returns `folder='/' name='capt0000.cr2'`). The app downloads and then
deletes every frame, so RAM is the ideal target anyway.

---

## Setting tables (exact gphoto2 labels)

The labels below are the exact strings gphoto2 reports/accepts for this body.
ISO/shutter tables live in `camera_tables.py` and are ordered there
**dark → bright** for the ramp math. Aperture labels are read from
`/main/capturesettings/aperture` at runtime for the mounted lens; the
`camera_tables.py` aperture list is now a fallback.

* **ISO** (Auto removed): `100, 125, 160, 200, 250, 320, 400, 500, 640, 800,
  1000, 1250, 1600, 2000, 2500, 3200, 4000, 5000, 6400, 8000, 10000, 12800,
  16000, 20000, 25600, 32000`.
* **Aperture** (lens-dependent; f/2.8 zoom shown): `2.8, 3.2, 3.5, 4, 4.5, 5,
  5.6, 6.3, 7.1, 8, 9, 10, 11, 13, 14, 16, 18, 20, 22`.
  Note `"2.8"` is the rounded label for √8 ≈ 2.828, so f/2.8 → f/4 is −1.03
  stops, not exactly −1.
* **Shutter**: whole-second labels (`30 … 0.3`) and fractional labels
  (`1/4 … 1/8000`). `shutter_label_to_seconds()` converts either form.

Brightness in stops:
`iso = log2(iso/100)`, `shutter = log2(seconds)`,
`aperture = -2·log2(f_number)`. Higher = brighter.

---

## Capture / download semantics

* `capture(GP_CAPTURE_IMAGE)` triggers the shutter and **blocks for the exposure
  duration** (up to 30 s at night), returning the on-camera folder/name.
* Frames are pulled with `file_get(...GP_FILE_TYPE_NORMAL)` then removed from the
  card with `file_delete(...)`. The app writes to a `.part` temp file and
  `os.replace()`s it into place so a half-downloaded CR2 is never analysed.
* A single PTP-IP session **cannot run two transactions at once**. Every camera
  operation (capture, download, delete, get/set config) is serialised through
  one re-entrant lock, `Camera.lock`. Ramp changes are therefore applied only in
  the safe window: after a capture, ≥ 1 s before the next one (see
  `pipeline.py`).

---

## Network gotchas observed

* Two active interfaces on the same `/24` (Wi-Fi + Ethernet) cause duplicate
  routes and mDNS on both; prefer a single interface while shooting.
* A stale `/etc/resolver/local` can hijack `.local` resolution and hide the
  camera from mDNS clients (it does **not** affect the app, which can be pointed
  at the raw IP).
* The camera sleeps/drops off Wi-Fi; the app polls the PTP port and waits for it
  to come back rather than crashing.

---

*Hand-maintained alongside the implementation. Run the e2e suite
(`uv run pytest -m e2e`) against a live body to re-verify these behaviours.*
