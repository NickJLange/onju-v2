# onju-flasher — containerized firmware toolchain

`docker/flash/` builds a self-contained image with a pinned arduino-cli
toolchain for compiling and flashing ESP32 firmware. No host-installed
arduino-cli is required; Podman is the only prerequisite.

## What the image contains

| Component | Version |
|---|---|
| arduino-cli | 1.2.2 |
| esp32 Arduino core (`esp32:esp32`) | 3.2.0 |
| Adafruit NeoPixel library | 1.12.3 |
| esp32_opus library | 1.0.3 |
| Base OS | debian:bookworm-slim |

Version pins live in `docker/flash/Containerfile` as `ARG` declarations.
Bump them deliberately; re-pin resolved digests after a bump (Task 1 Step 5 procedure).

## Building the image

```bash
podman build -t onju-flasher docker/flash
```

`flash.sh` auto-builds this image on first run (when `FLASH_RUNTIME=container`).
Rebuild manually after editing `Containerfile` or `entrypoint.sh`.

## How flash.sh uses the image

`flash.sh` selects the toolchain runtime via environment variables evaluated
before any podman invocation.

| Variable | Default | Effect |
|---|---|---|
| `FLASH_RUNTIME` | `container` | `container` — run toolchain in podman; `native` — call entrypoint.sh directly on the host |
| `FLASH_IMAGE` | `onju-flasher` | Image name (or tag) to run; change to use a custom build |
| `FLASH_DRYRUN` | `0` | Set to `1` to print the podman command without executing |
| `PODMAN_CONNECTION` | _(unset)_ | Passed as `--connection <value>` to every `podman` call; use to target a remote or named machine (see macOS section below) |
| `FLASH_SKIP_CREDS` | `0` | Set to `1` to skip WiFi credential generation (useful in CI) |
| `FLASH_OS` | `$(uname -s)` lowercased | Override OS detection (`darwin` or `linux`) |
| `WIFI_SSID` | _(interactive)_ | WiFi network name; bypasses interactive prompt |
| `WIFI_PASSWORD` | _(interactive)_ | WiFi password; bypasses interactive prompt |

### Common invocations

```bash
# Compile + upload + open monitor (default target: onjuino)
./flash.sh

# Flash the M5 Echo target
./flash.sh m5_echo

# Compile only (no device needed); works on any host including macOS
./flash.sh m5_echo compile

# Force-regenerate WiFi credentials
./flash.sh --regen

# Flash to an explicit port, skip monitor
./flash.sh m5_echo /dev/ttyUSB0 --no-monitor

# Dry-run: see the podman command without executing
FLASH_DRYRUN=1 ./flash.sh m5_echo

# Use a pre-built custom image
FLASH_IMAGE=my-onju-flasher:dev ./flash.sh

# Native mode — use host arduino-cli instead of container
FLASH_RUNTIME=native ./flash.sh
```

## Linux rootless device mapping

When `FLASH_RUNTIME=container`, `flash.sh` passes the serial device into the
container with:

```
--device <dev>:<dev> --group-add keep-groups
```

`--device` bind-mounts the character device node into the container namespace.
`--group-add keep-groups` carries the caller's supplementary group memberships
into the container so the device's group permission is honoured without
running as root.

**Prerequisite:** your Linux user must be a member of the group that owns the
serial device — usually `dialout` (Debian/Ubuntu) or `uucp` (Arch/Fedora).

```bash
# Debian/Ubuntu
sudo usermod -aG dialout "$USER"   # then log out and back in

# Arch / Fedora
sudo usermod -aG uucp "$USER"
```

Verify with:

```bash
ls -la /dev/ttyUSB0   # or ttyACM0 / the device you see
groups                 # must include dialout or uucp
```

---

## macOS — optional QEMU upload appendix

`compile` works on macOS via the default applehv/libkrun machine, but
**uploading firmware requires USB pass-through**, which applehv and libkrun do
not expose. The solution is a QEMU-backed machine with explicit USB vendor/
product filtering.

Why QEMU and not libkrun/applehv: see
`docs/superpowers/specs/2026-06-28-containerized-firmware-flashing-design.md` (Research).

### Find the USB vendor/product IDs

The M5 Atom Echo uses a CH9102 USB-serial chip (vendor `1a86`). Confirm the
product ID for your unit:

```bash
system_profiler SPUSBDataType | grep -A4 "CH9102"
# or: brew install lsusb && lsusb | grep 1a86
```

Note the product ID (e.g. `55d4`).

### Create and start the QEMU machine

Run these once (or after `podman machine rm flasher`):

```bash
brew install qemu

podman machine init --cpus 2 --memory 4096 --rootful --provider qemu \
  --usb vendor=1a86,product=<PID>  flasher      # M5 = CH9102, vendor 1a86; confirm PID

podman machine start flasher
```

Replace `<PID>` with the product ID from the step above.

### Flash via the QEMU machine

```bash
PODMAN_CONNECTION=flasher ./flash.sh m5_echo    # build+flash via the QEMU machine
```

`PODMAN_CONNECTION=flasher` routes every `podman` call through the named
machine. The `--usb` filter means the CH9102 device is visible inside the
machine as a standard `/dev/ttyUSB*` node, and `detect_device` in `flash.sh`
finds it automatically.

### Stop the machine when done

```bash
podman machine stop flasher
```

The machine persists across reboots; `podman machine start flasher` reattaches
it (USB pass-through requires the device to already be plugged in at `start`
time when using QEMU).
