# Containerized firmware flashing (podman) — design

**Status:** approved-pending-review · **Date:** 2026-06-28 · **Repo:** `onju-v2`

## Context

`flash.sh` compiles and uploads the ESP32 firmware (onjuino / M5 ATOM Echo) using
a host-local `arduino-cli` plus the `esp32:esp32` core and two libraries. That
means every machine that flashes needs the exact toolchain installed and in sync,
and "works on my Mac" drifts from "works on the Linux box." We want the toolchain
to live in a **rootless podman container** that builds on Linux or macOS, with the
M5's USB serial port mapped in, so flashing is reproducible and the host only needs
podman.

Primary flashing host is **Linux** (confirmed acceptable by the owner), where USB
passthrough to a rootless container is clean. macOS is supported for *compiling*
everywhere and for *uploading* only via an optional dedicated QEMU machine (see
Research below for why).

## Goals

- Toolchain (arduino-cli + esp32 core + libs) runs in a **reproducible, version-pinned**
  podman image — no host arduino-cli required.
- **Rootless** on Linux; the M5 serial device is mapped into the container.
- Same operator CLI surface as today: `flash.sh m5_echo`, `compile`, `--regen`, an
  optional explicit port.
- Cross-platform image build (Linux + macOS).

## Non-goals

- Replacing the macOS host-native convenience entirely. The existing host path keeps
  working; the container is the new reproducible backend.
- Roaming / network flashing, USB/IP. Out of scope (see Future).
- Containerizing credential *generation* (inherently host-side — see Architecture).

## Decisions locked in

- **Approach A** — thin host wrapper + pure-toolchain container.
- **Linux = primary** flash host: rootless `podman run --device … --group-add keep-groups`.
- **macOS = compile-anywhere, upload via optional dedicated QEMU `flasher` machine**
  (`--provider qemu --usb vendor=…,product=…`, rootful). Documented, not the critical
  path, because the owner flashes from Linux.
- **Credentials are rendered on the host**, never fetched inside the container.

---

## Research findings — USB passthrough on podman (revisit ~2026-12)

Encoded so we don't re-derive this in six months. **Bottom line: on macOS, host USB
passthrough into a podman machine is QEMU-only. libkrun/krunkit cannot do it, by
design — not a missing feature we can wait a point-release for.**

| Provider | Host USB passthrough? | Notes |
|---|---|---|
| **Linux (no VM)** | ✅ native | `--device /dev/ttyUSB0` straight into the container. Rootless needs `--group-add keep-groups` (preserve `dialout`/`uucp`) and optionally `--device-cgroup-rule='c 188:* rwm'` (188 = USB-serial major; 166 = ttyACM). Prefer `/dev/serial/by-id/*` for stable naming. |
| **macOS — QEMU** | ✅ via `podman machine init --usb` | podman docs: `--usb` is **"Only supported for QEMU Machines."** Accepts `vendor=HEX,product=HEX` (stable) or `bus=N,devnum=N` (changes on replug). Set at **init** time. |
| **macOS — applehv** | ❌ | Apple Hypervisor.framework provider; no podman `--usb` support. (Our box's current default: podman 5.8.2, `applehv`.) |
| **macOS — libkrun/krunkit** | ❌ | krunkit `--device` configures **virtio only** (`virtio-blk`, `virtio-net`, `virtio-serial`, `virtio-vsock`, `virtio-fs`). `virtio-serial` redirects the VM's *own console*, not a host USB serial adapter. No `--usb`/vfio/hidraw. libkrun's design explicitly treats host **"device passthrough [as] off the table"**, using paravirtualization (that's how virtio-gpu gives GPU access without passthrough). Checked the krunkit usage docs directly on 2026-06-28 — not a recent regression, it's intentional. |

**Trend caution:** Podman Desktop 1.19+ / podman on Apple Silicon is moving the
*default* provider to libkrun/krunkit (for GPU/AI). That does **not** add USB; it
just means QEMU is no longer default. QEMU remains selectable (`--provider qemu`,
`brew install qemu`), so our dedicated `flasher` machine is unaffected — but if a
future podman drops the QEMU provider on macOS, the macOS *upload* path dies and
Linux flashing becomes mandatory. Re-check at revisit.

**Sources (captured 2026-06-28):**
- podman `machine init` man page — `--usb` "Only supported for QEMU Machines" — https://docs.podman.io/en/latest/markdown/podman-machine-init.1.html
- krunkit usage docs (virtio-only `--device`) — https://raw.githubusercontent.com/containers/krunkit/main/docs/usage.md
- libkrun GPU via paravirtualization, "passthrough off the table" — https://sinrega.org/2024-03-06-enabling-containers-gpu-macos/
- Red Hat Developer, krunkit/libkrun on macOS — https://developers.redhat.com/articles/2025/06/05/how-we-improved-ai-inference-macos-podman-containers
- USB-in-podman Linux flags (`--group-add keep-groups`, cgroup rule) — https://oneuptime.com/blog/post/2026-03-18-pass-usb-devices-podman-containers/view

---

## Architecture

Two responsibilities, cleanly split:

**Host wrapper (`flash.sh`, refactored).** Keeps only what is inherently host-side:
1. **Credential rendering** → writes `<target>/credentials.h` from the template.
   - macOS: existing Keychain + `networksetup` flow (unchanged UX).
   - Linux: `WIFI_SSID`/`WIFI_PASSWORD` env, a `.env`, or interactive prompt (no Keychain).
   Secrets are rendered into the header on the host; the container only ever sees the
   already-rendered file.
2. **`git_hash.h`** generation (host `git`).
3. **podman orchestration**: ensure the image exists (build if missing), detect the
   serial device, then `podman run` the toolchain container with the repo bind-mounted
   and the device mapped.

**Toolchain container (`docker/flash/`).** A pure, reproducible image: a slim Linux
base + pinned `arduino-cli` + pre-installed `esp32:esp32` core + pinned libraries
(`Adafruit NeoPixel`, `esp32_opus`). Its entrypoint takes `(target, action, port)`
and runs `arduino-cli compile` / `arduino-cli upload` / monitor against the mounted
repo (`/work`) and the mapped device. **No secrets, no sketch, no core downloads at
runtime** — everything heavy is baked at build time.

### Data flow (Linux, the primary path)

```text
flash.sh (host)                          podman container (toolchain)
  render credentials.h  ─┐
  write git_hash.h       ├─ bind-mount repo → /work
  detect /dev/serial/by-id/XXX           arduino-cli compile  (uses baked core/libs)
  podman run \                           arduino-cli upload  → /dev/ttyUSB0 (mapped)
    --device $DEV --group-add keep-groups   serial monitor (optional, -it)
    -v $REPO:/work  flasher  <target> <action>
```

### Invocation surface (unchanged for the operator)

```bash
./flash.sh m5_echo            # render creds, ensure image, compile + upload + monitor in container
./flash.sh m5_echo compile    # compile only (no device needed — works on any host incl. macOS/applehv)
./flash.sh m5_echo --regen    # re-render WiFi credentials
./flash.sh m5_echo /dev/ttyUSB0   # force a device node
```

Internals: a `FLASH_RUNTIME` selector (`container` default, `native` fallback for a
host that already has arduino-cli) decides whether toolchain steps run in the
container or directly. Container targeting on macOS uses `podman --connection flasher`.

## Components / files

| Action | Path | Purpose |
|---|---|---|
| New | `docker/flash/Containerfile` | Pinned toolchain image (arduino-cli, esp32 core, libs) |
| New | `docker/flash/entrypoint.sh` | In-container: compile / upload / monitor against `/work` + mapped device |
| New | `docker/flash/README.md` | Build/run, device-mapping, the macOS QEMU `flasher` recipe |
| Edit | `flash.sh` | Refactor to host-wrapper: credential render + `git_hash` + podman orchestration; keep CLI surface; `native` fallback |
| Edit | `docs/m5-getting-started.md` | Add the containerized flow (Linux primary) alongside host-native |

## Reproducibility (the point of the 6-month revisit)

- The Containerfile **pins exact versions**: `arduino-cli`, the `esp32:esp32` core,
  and each library. These are recorded in the Containerfile and bumped deliberately,
  so a rebuild in six months produces the same firmware.
- Image tag encodes the core version (e.g. `onju-flasher:esp32-<coreversion>`).
- The M5 ATOM Echo enumerates as a **CH9102 USB-serial bridge (vendor `1a86`)**;
  confirm the exact `product` per unit with `lsusb` (Linux) / `system_profiler
  SPUSBDataType` (macOS) before setting `--usb vendor=…,product=…`.

## macOS upload path (optional appendix, not critical path)

Because the owner flashes from Linux, macOS *upload-in-container* is documented but
not automated as the default:

```bash
brew install qemu
podman machine init --cpus 2 --memory 4096 --rootful --provider qemu \
  --usb vendor=1a86,product=<PID>  flasher
podman machine start flasher
# build + run target the flasher connection:
podman --connection flasher build -t onju-flasher docker/flash
podman --connection flasher run --device /dev/ttyUSB0 -v "$PWD":/work onju-flasher m5_echo upload
podman machine stop flasher        # cheap at idle
```

`--rootful` is on the *machine* (root inside the VM), which sidesteps the
`dialout`/`keep-groups` permission dance for the mapped node. It is **not** host root —
"rootless" still holds at the macOS host layer. Watch item: ESP32 toggles DTR/RTS and
may re-enumerate mid-flash; by `vendor/product` QEMU should re-grab it, but this is the
most likely rough edge.

## Verification

- **Compile-in-container, no device** (runs on Linux *and* macOS/applehv):
  `./flash.sh m5_echo compile` → produces `m5_echo/build/*.ino.bin`. CI-friendly.
- **Reproducibility:** two clean builds of the image yield the same core/lib versions
  (`arduino-cli core list`, `arduino-cli lib list` inside the image match the pins).
- **Linux upload (primary):** with the M5 attached, `./flash.sh m5_echo` compiles,
  uploads, and the serial monitor shows WiFi join + multicast announce. The
  device-bringup round trip then follows `docs/m5-getting-started.md`.
- **Permissions:** confirm rootless upload works with `--group-add keep-groups` and
  fails cleanly (clear message) when the user lacks `dialout`/`uucp`.
- **macOS optional:** the `flasher`-machine recipe uploads successfully (manual,
  not gated in CI).

## Future / out of scope

- Automating the macOS `flasher` machine lifecycle inside `flash.sh` (only worth it if
  Mac flashing becomes routine).
- USB/IP or any network-attached device flashing.
- Re-evaluating libkrun USB support at the ~2026-12 revisit (see Research).
