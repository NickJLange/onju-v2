# Containerized Firmware Flashing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the ESP32 firmware toolchain (arduino-cli + esp32 core + libs) in a rootless podman container so flashing is reproducible, with the M5's USB serial port mapped in — Linux primary, macOS compile-anywhere + optional QEMU upload.

**Architecture:** A thin host wrapper (`flash.sh`, refactored) keeps the inherently host-side concerns — WiFi credential rendering, `git_hash.h`, and podman orchestration — and delegates compile/upload/monitor to a pinned, secret-free toolchain image (`docker/flash/`). On Linux the serial device is mapped rootless via `--device` + `--group-add keep-groups`; on macOS compile works under any provider while upload needs an optional dedicated QEMU `flasher` machine.

**Tech Stack:** bash, podman (rootless), arduino-cli, esp32:esp32 Arduino core, Debian-slim container.

## Global Constraints

- **Reproducibility:** the Containerfile pins exact versions of `arduino-cli`, the `esp32:esp32` core, and every library (`Adafruit NeoPixel`, `esp32_opus`). Pins are recorded as `ARG` defaults and bumped deliberately.
- **No secrets in the image or repo history:** `credentials.h` is rendered on the host and only bind-mounted; it is never `COPY`'d into the image. (`credentials.h` is already git-ignored — keep it so.)
- **Rootless on Linux**; `--group-add keep-groups` is required for the mapped serial node to be writable.
- **Operator CLI surface is unchanged:** `./flash.sh m5_echo`, `./flash.sh m5_echo compile`, `./flash.sh m5_echo --regen`, `./flash.sh m5_echo /dev/ttyUSB0`.
- **macOS USB upload is QEMU-only** (`podman machine init --usb` is QEMU-only; libkrun/applehv cannot pass host USB). See the design doc's Research table. Linux is the primary flash host.
- **FQBNs (must match across host and container):** onjuino = `esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,UploadSpeed=115200`; m5_echo = `esp32:esp32:m5stack_atom:UploadSpeed=1500000`.

---

### Task 1: Toolchain image — Containerfile + entrypoint

**Files:**
- Create: `docker/flash/Containerfile`
- Create: `docker/flash/entrypoint.sh`

**Interfaces:**
- Produces: an image (built locally as `onju-flasher`) whose entrypoint is invoked as
  `flash-entrypoint <target> <action> [port]`, where `target ∈ {onjuino, m5_echo}`,
  `action ∈ {compile, upload, flash, monitor}`. Compiles into `/work/<target>/build`,
  uploads/monitors against `[port]`. Reads the sketch + `credentials.h` from the
  bind-mounted `/work`.

- [ ] **Step 1: Write the container entrypoint**

Create `docker/flash/entrypoint.sh`:

```bash
#!/usr/bin/env bash
# Toolchain entrypoint — runs INSIDE the container against the bind-mounted /work.
# Usage: flash-entrypoint <target> <action> [port]
set -euo pipefail

TARGET="${1:-m5_echo}"
ACTION="${2:-flash}"     # compile | upload | flash (compile+upload) | monitor
PORT="${3:-}"

case "$TARGET" in
    onjuino)
        FQBN="esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,UploadSpeed=115200"
        DIR="/work/onjuino"; INO="onjuino.ino" ;;
    m5_echo|m5echo)
        FQBN="esp32:esp32:m5stack_atom:UploadSpeed=1500000"
        DIR="/work/m5_echo"; INO="m5_echo.ino" ;;
    *) echo "entrypoint: unknown target '$TARGET'" >&2; exit 2 ;;
esac
BUILD="$DIR/build"

require_port() { [ -n "$PORT" ] || { echo "entrypoint: '$ACTION' needs a port" >&2; exit 2; }; }
do_compile() { arduino-cli compile --fqbn "$FQBN" --build-path "$BUILD" "$DIR/$INO"; }
do_upload()  { require_port; arduino-cli upload --fqbn "$FQBN" --port "$PORT" --input-dir "$BUILD" "$DIR/$INO"; }
do_monitor() { require_port; arduino-cli monitor -p "$PORT" -c baudrate=115200; }

case "$ACTION" in
    compile) do_compile ;;
    upload)  do_upload ;;
    flash)   do_compile; do_upload ;;
    monitor) do_monitor ;;
    *) echo "entrypoint: unknown action '$ACTION'" >&2; exit 2 ;;
esac
```

- [ ] **Step 2: Write the Containerfile**

Create `docker/flash/Containerfile`:

```dockerfile
FROM debian:bookworm-slim

# --- Reproducibility pins (bump deliberately; see Step 5 to lock resolved versions) ---
ARG ARDUINO_CLI_VERSION=1.2.2
ARG ESP32_CORE_VERSION=3.2.0
ARG NEOPIXEL_VERSION=1.12.3
ARG ESP32_OPUS_VERSION=        # locked in Step 5 after first resolve

ENV ARDUINO_DIRECTORIES_DATA=/opt/arduino/data \
    ARDUINO_DIRECTORIES_DOWNLOADS=/opt/arduino/downloads \
    ARDUINO_DIRECTORIES_USER=/opt/arduino/user

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl python3 \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
        | BINDIR=/usr/local/bin sh -s ${ARDUINO_CLI_VERSION}

RUN arduino-cli config init \
    && arduino-cli config set network.connection_timeout 600s \
    && arduino-cli config add board_manager.additional_urls \
         https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json \
    && arduino-cli core update-index \
    && arduino-cli core install esp32:esp32@${ESP32_CORE_VERSION} \
    && arduino-cli lib install "Adafruit NeoPixel@${NEOPIXEL_VERSION}" \
    && arduino-cli lib install "esp32_opus${ESP32_OPUS_VERSION:+@${ESP32_OPUS_VERSION}}"

WORKDIR /work
COPY entrypoint.sh /usr/local/bin/flash-entrypoint
RUN chmod +x /usr/local/bin/flash-entrypoint
ENTRYPOINT ["/usr/local/bin/flash-entrypoint"]
```

- [ ] **Step 3: Build the image**

Run: `podman build -t onju-flasher docker/flash`
Expected: build completes; final line `Successfully tagged localhost/onju-flasher:latest` (or the bare image id). If `ARDUINO_CLI_VERSION`/`ESP32_CORE_VERSION` 404s, the `core install` step fails loudly — bump the `ARG` to an existing release and rebuild.

- [ ] **Step 4: Verify the toolchain resolved**

Run: `podman run --rm --entrypoint arduino-cli onju-flasher core list`
Expected: a row `esp32:esp32   3.2.0   ...`.
Run: `podman run --rm --entrypoint arduino-cli onju-flasher lib list`
Expected: rows for `Adafruit NeoPixel` and `esp32_opus`.

- [ ] **Step 5: Lock the resolved esp32_opus version**

Take the `esp32_opus` version printed in Step 4, set it as the `ESP32_OPUS_VERSION` ARG default in the Containerfile, and rebuild to confirm the pin resolves.
Run: `podman build -t onju-flasher docker/flash && podman run --rm --entrypoint arduino-cli onju-flasher lib list | grep esp32_opus`
Expected: same version, now pinned.

- [ ] **Step 6: Commit**

```bash
git add docker/flash/Containerfile docker/flash/entrypoint.sh
git commit -m "feat(flash): pinned podman toolchain image for ESP32 firmware"
```

---

### Task 2: Container dispatch + dry-run seam in flash.sh (compile path)

Refactor `flash.sh` so toolchain steps run through podman by default, with a testable
dry-run that prints the assembled command instead of executing it. This task wires the
**compile** action end-to-end (no device needed — verifiable on this Mac).

**Files:**
- Modify: `flash.sh` (replace the compile/port/upload/monitor tail, lines ~154–232, and add helpers)
- Test: `tests/test_flash_dryrun.sh` (new)

**Interfaces:**
- Consumes: the `onju-flasher` image and entrypoint contract from Task 1.
- Produces: env-configurable behavior — `FLASH_RUNTIME` (`container` default | `native`),
  `FLASH_IMAGE` (default `onju-flasher`), `FLASH_DRYRUN` (`1` prints the podman command and
  exits 0 without running), `PODMAN_CONNECTION` (passed as `--connection`). A shell function
  `run_toolchain <action>` that assembles and runs/prints the invocation.

- [ ] **Step 1: Write the failing dry-run test**

Create `tests/test_flash_dryrun.sh`:

```bash
#!/usr/bin/env bash
# Asserts flash.sh assembles the right podman command without running anything.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "FAIL: $1" >&2; exit 1; }

# Compile action: must run the image with the repo mounted and target+action args,
# and must NOT map a device (compile needs no port).
out="$(FLASH_DRYRUN=1 FLASH_SKIP_CREDS=1 ./flash.sh m5_echo compile)"
echo "$out" | grep -q "podman" || fail "no podman in compile dry-run"
echo "$out" | grep -q -- "-v .*:/work" || fail "repo not bind-mounted"
echo "$out" | grep -Eq "onju-flasher +m5_echo +compile" || fail "target/action args wrong"
echo "$out" | grep -q -- "--device" && fail "compile must not map a device"

echo "PASS: flash.sh compile dry-run assembles correctly"
```

Make it executable: `chmod +x tests/test_flash_dryrun.sh`

- [ ] **Step 2: Run the test to verify it fails**

Run: `./tests/test_flash_dryrun.sh`
Expected: FAIL (flash.sh doesn't yet honor `FLASH_DRYRUN`/`FLASH_SKIP_CREDS` and still runs arduino-cli directly).

- [ ] **Step 3: Add the runtime helpers to flash.sh**

In `flash.sh`, immediately after the target `case` block (after the line that sets
`BUILD_DIR="$PROJECT_DIR/build"`, ~line 42), insert:

```bash
# -------------------------------------------------------
# Toolchain runtime: container (default) or native
# -------------------------------------------------------
FLASH_RUNTIME="${FLASH_RUNTIME:-container}"
FLASH_IMAGE="${FLASH_IMAGE:-onju-flasher}"
FLASH_DRYRUN="${FLASH_DRYRUN:-0}"

ensure_image() {
    [ "$FLASH_RUNTIME" = "container" ] || return 0
    if ! podman ${PODMAN_CONNECTION:+--connection "$PODMAN_CONNECTION"} \
            image exists "$FLASH_IMAGE" 2>/dev/null; then
        echo "Building $FLASH_IMAGE (first run)..."
        podman ${PODMAN_CONNECTION:+--connection "$PODMAN_CONNECTION"} \
            build -t "$FLASH_IMAGE" "$REPO/docker/flash"
    fi
}

# run_toolchain <action> [container_port]
# Container port is the device path AS SEEN IN THE CONTAINER (set by Task 4).
run_toolchain() {
    local action="$1" cport="${2:-}"
    if [ "$FLASH_RUNTIME" = "native" ]; then
        "$REPO/docker/flash/entrypoint.sh" "$TARGET" "$action" "$cport"
        return
    fi
    local tty=""; [ -t 0 ] && tty="-it"
    # DEVICE_ARGS is populated by Task 4 for upload/monitor; empty for compile.
    local cmd=(podman)
    [ -n "${PODMAN_CONNECTION:-}" ] && cmd+=(--connection "$PODMAN_CONNECTION")
    cmd+=(run --rm $tty -v "$REPO:/work" ${DEVICE_ARGS:-} "$FLASH_IMAGE" "$TARGET" "$action" "$cport")
    if [ "$FLASH_DRYRUN" = "1" ]; then
        printf '%s ' "${cmd[@]}"; printf '\n'
        return 0
    fi
    "${cmd[@]}"
}
```

- [ ] **Step 4: Replace the compile/upload/monitor tail with run_toolchain calls**

In `flash.sh`, replace everything from `# Check if compile is needed` (~line 154)
through the end of the file with:

```bash
# -------------------------------------------------------
# Ensure image, then dispatch the requested action
# -------------------------------------------------------
ensure_image

if [ "$COMPILE_ONLY" = true ]; then
    echo "Compile-only mode"
    run_toolchain compile
    exit 0
fi

# DEVICE_ARGS + CONTAINER_PORT are set by detect_device (Task 4). For now, no device.
DEVICE_ARGS=""
CONTAINER_PORT=""

echo ""
echo "Flashing $TARGET..."
run_toolchain flash "$CONTAINER_PORT"

if [ "$NO_MONITOR" != true ]; then
    echo "Starting serial monitor..."
    run_toolchain monitor "$CONTAINER_PORT"
fi
```

(Task 4 inserts `detect_device` and real `DEVICE_ARGS`/`CONTAINER_PORT` before the flash call. The `FORCE_COMPILE`/timestamp skip logic is intentionally dropped — the container build cache and arduino-cli's own incremental build handle this.)

- [ ] **Step 5: Gate credential rendering behind FLASH_SKIP_CREDS (test hook)**

Find the credentials block (`if [ -f "$OUTPUT" ] && [ "$REGEN" = false ]; then ...`,
~line 92) and add this guard as its first line inside the block region — i.e. wrap the
whole credential section so tests can bypass it. Immediately before that `if`, insert:

```bash
if [ "${FLASH_SKIP_CREDS:-0}" != "1" ]; then
```

and immediately after the credential section's closing `fi` (the one before
`echo ""` / the `git_hash.h` section, ~line 140), insert:

```bash
fi  # FLASH_SKIP_CREDS
```

- [ ] **Step 6: Run the dry-run test to verify it passes**

Run: `./tests/test_flash_dryrun.sh`
Expected: `PASS: flash.sh compile dry-run assembles correctly`

- [ ] **Step 7: Real compile-in-container (works on this Mac under applehv)**

Run: `FLASH_SKIP_CREDS=1 printf '#define WIFI_SSID "x"\n#define WIFI_PASSWORD "y"\n' > m5_echo/credentials.h; ./flash.sh m5_echo compile`
Expected: image builds if needed, then `arduino-cli compile` runs in-container and writes `m5_echo/build/m5_echo.ino.bin`. Verify: `ls m5_echo/build/*.ino.bin`.

- [ ] **Step 8: Commit**

```bash
git add flash.sh tests/test_flash_dryrun.sh
git commit -m "feat(flash): run toolchain via podman with dry-run seam (compile path)"
```

---

### Task 3: Cross-platform credential rendering

Today the credential step is macOS-only (Keychain + `networksetup`). Add a Linux branch
(env vars or prompt) so the host wrapper renders `credentials.h` on either OS before
handing the repo to the container.

**Files:**
- Modify: `flash.sh` (credentials section, ~lines 92–140)
- Test: `tests/test_flash_creds.sh` (new)

**Interfaces:**
- Consumes: `credentials.h.template` in each target dir (existing `{{WIFI_SSID}}` /
  `{{WIFI_PASSWORD}}` placeholders).
- Produces: a rendered `<target>/credentials.h`. On Linux, reads `WIFI_SSID` and
  `WIFI_PASSWORD` from the environment; prompts if unset and a TTY is present.

- [ ] **Step 1: Write the failing Linux-credentials test**

Create `tests/test_flash_creds.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $1" >&2; exit 1; }

rm -f m5_echo/credentials.h
# Force the non-macOS path and supply creds via env; dry-run skips podman.
out="$(FLASH_OS=linux WIFI_SSID=testnet WIFI_PASSWORD=secret123 \
       FLASH_DRYRUN=1 ./flash.sh m5_echo compile)"
[ -f m5_echo/credentials.h ] || fail "credentials.h not rendered"
grep -q 'testnet' m5_echo/credentials.h || fail "SSID not substituted"
grep -q 'secret123' m5_echo/credentials.h || fail "password not substituted"
echo "PASS: linux credential rendering"
```

Make executable: `chmod +x tests/test_flash_creds.sh`

- [ ] **Step 2: Run it to verify it fails**

Run: `./tests/test_flash_creds.sh`
Expected: FAIL (no Linux branch yet; on macOS it would try Keychain).

- [ ] **Step 3: Add an OS selector and Linux credential branch**

In `flash.sh`, near the top (after `REPO=...`), add:

```bash
FLASH_OS="${FLASH_OS:-$(uname -s | tr '[:upper:]' '[:lower:]')}"  # darwin | linux
```

Then inside the credentials section, replace the macOS-specific SSID/password discovery
(the block that calls `networksetup` / `security find-generic-password`) with an
OS switch. The structure becomes:

```bash
if [ -f "$OUTPUT" ] && [ "$REGEN" = false ]; then
    echo "Using existing credentials.h (pass --regen to regenerate)"
else
    WIFI_SSID="${WIFI_SSID:-}"
    WIFI_PASSWORD="${WIFI_PASSWORD:-}"

    if [ "$FLASH_OS" = "darwin" ]; then
        # ---- existing macOS Keychain + networksetup discovery (unchanged) ----
        # (keep the current networksetup SSID detection and
        #  `security find-generic-password -wa "$WIFI_SSID"` logic here)
        :
    fi

    # ---- cross-platform fallback / Linux path ----
    [ -z "$WIFI_SSID" ] && [ -t 0 ] && read -rp "WiFi SSID: " WIFI_SSID
    [ -z "$WIFI_SSID" ] && { echo "ERROR: No WiFi SSID (set WIFI_SSID)"; exit 1; }
    if [ -z "$WIFI_PASSWORD" ] && [ -t 0 ]; then
        read -rsp "WiFi password: " WIFI_PASSWORD; echo ""
    fi
    [ -z "$WIFI_PASSWORD" ] && { echo "ERROR: No WiFi password (set WIFI_PASSWORD)"; exit 1; }

    sed -e "s|{{WIFI_SSID}}|${WIFI_SSID}|g" \
        -e "s|{{WIFI_PASSWORD}}|${WIFI_PASSWORD}|g" \
        "$TEMPLATE" > "$OUTPUT"
    echo "Generated credentials.h"
fi
```

Keep the existing macOS discovery code verbatim inside the `if [ "$FLASH_OS" = "darwin" ]` block; the env vars `WIFI_SSID`/`WIFI_PASSWORD` it sets then flow into the shared `sed` render.

- [ ] **Step 4: Run the creds test to verify it passes**

Run: `./tests/test_flash_creds.sh`
Expected: `PASS: linux credential rendering`

- [ ] **Step 5: Re-run the dry-run test (no regression)**

Run: `./tests/test_flash_dryrun.sh`
Expected: still `PASS`.

- [ ] **Step 6: Commit**

```bash
git add flash.sh tests/test_flash_creds.sh
git commit -m "feat(flash): cross-platform WiFi credential rendering (linux env/prompt)"
```

---

### Task 4: Linux device detection, upload, and monitor

Map the M5 serial device rootless into the container for `upload`/`monitor`.

**Files:**
- Modify: `flash.sh` (add `detect_device`, populate `DEVICE_ARGS`/`CONTAINER_PORT` before the flash call)
- Test: `tests/test_flash_dryrun.sh` (extend with an upload-path assertion)

**Interfaces:**
- Consumes: a host serial node (explicit `/dev/...` arg, or auto-detected
  `/dev/serial/by-id/*` → `/dev/ttyUSB*` → `/dev/ttyACM*` on Linux; `/dev/cu.usbserial-*`
  on macOS-native).
- Produces: `DEVICE_ARGS` (the `--device ... --group-add keep-groups` fragment) and
  `CONTAINER_PORT` (device path inside the container) consumed by `run_toolchain` (Task 2).

- [ ] **Step 1: Extend the dry-run test for the upload path**

Append to `tests/test_flash_dryrun.sh` (before the final `echo "PASS"` line of the file,
add a second scenario as its own check):

```bash
# Upload action with an explicit device: must map it and add keep-groups.
out2="$(FLASH_DRYRUN=1 FLASH_SKIP_CREDS=1 FLASH_OS=linux \
        ./flash.sh m5_echo /dev/ttyUSB0)"
echo "$out2" | grep -q -- "--device /dev/ttyUSB0" || fail "device not mapped on upload"
echo "$out2" | grep -q -- "--group-add keep-groups" || fail "keep-groups missing"
echo "$out2" | grep -Eq "onju-flasher +m5_echo +flash +/dev/ttyUSB0" || fail "upload args wrong"
echo "PASS: flash.sh upload dry-run maps device rootless"
```

- [ ] **Step 2: Run it to verify the upload check fails**

Run: `./tests/test_flash_dryrun.sh`
Expected: FAIL at "device not mapped on upload" (no detection/mapping yet).

- [ ] **Step 3: Add detect_device and wire it before the flash call**

In `flash.sh`, add this helper alongside the Task 2 helpers:

```bash
# Sets DEVICE_ARGS and CONTAINER_PORT for upload/monitor. $1 = explicit port or "".
detect_device() {
    local explicit="$1" dev=""
    if [ -n "$explicit" ]; then
        dev="$explicit"
    elif [ "$FLASH_OS" = "darwin" ]; then
        for g in /dev/cu.usbserial-* /dev/cu.usbmodem*; do
            [ -e "$g" ] && { dev="$g"; break; }
        done
    else
        for g in /dev/serial/by-id/* /dev/ttyUSB* /dev/ttyACM*; do
            [ -e "$g" ] && { dev="$g"; break; }
        done
    fi
    [ -n "$dev" ] || { echo "ERROR: no serial device found (pass one explicitly)"; exit 1; }
    CONTAINER_PORT="$dev"
    if [ "$FLASH_RUNTIME" = "container" ]; then
        DEVICE_ARGS="--device ${dev}:${dev} --group-add keep-groups"
    else
        DEVICE_ARGS=""
    fi
    echo "Using device: $dev"
}
```

Then in the dispatch tail (Task 2, Step 4), replace the two placeholder lines
`DEVICE_ARGS=""` / `CONTAINER_PORT=""` with:

```bash
DEVICE_ARGS=""
CONTAINER_PORT=""
detect_device "$PORT"
```

(`$PORT` is the existing explicit-port variable parsed from args.)

- [ ] **Step 4: Run the dry-run test to verify both paths pass**

Run: `./tests/test_flash_dryrun.sh`
Expected: both `PASS` lines print.

- [ ] **Step 5: Real upload + monitor on a Linux host with the M5 attached**

On the Linux flash host (M5 plugged in):
Run: `WIFI_SSID=... WIFI_PASSWORD=... ./flash.sh m5_echo`
Expected: compiles in-container, uploads to the detected `/dev/ttyUSB*`, then the serial
monitor shows the boot log (WiFi join, multicast announce). If upload fails with a
permission error, confirm your user is in `dialout`/`uucp` (that's what `keep-groups`
forwards).

- [ ] **Step 6: Commit**

```bash
git add flash.sh tests/test_flash_dryrun.sh
git commit -m "feat(flash): rootless device mapping for upload/monitor (linux)"
```

---

### Task 5: Documentation — container flow + macOS QEMU appendix

**Files:**
- Create: `docker/flash/README.md`
- Modify: `docs/m5-getting-started.md` (add the containerized flow)

**Interfaces:** none (docs). Commands must match the flags implemented in Tasks 1–4.

- [ ] **Step 1: Write docker/flash/README.md**

Create `docker/flash/README.md` covering: what the image contains and the version pins;
`podman build -t onju-flasher docker/flash`; how `flash.sh` uses it (`FLASH_RUNTIME`,
`FLASH_IMAGE`, `FLASH_DRYRUN`, `PODMAN_CONNECTION`); the Linux rootless device mapping
(`--device` + `--group-add keep-groups`, `dialout`/`uucp` note); and the **macOS optional
QEMU upload appendix** verbatim from the design doc:

```bash
brew install qemu
podman machine init --cpus 2 --memory 4096 --rootful --provider qemu \
  --usb vendor=1a86,product=<PID>  flasher      # M5 = CH9102, vendor 1a86; confirm PID
podman machine start flasher
PODMAN_CONNECTION=flasher ./flash.sh m5_echo    # build+flash via the QEMU machine
podman machine stop flasher
```

Include the one-line rationale + a pointer to the research table:
`Why QEMU and not libkrun/applehv: see docs/superpowers/specs/2026-06-28-containerized-firmware-flashing-design.md (Research).`

- [ ] **Step 2: Add the containerized flow to docs/m5-getting-started.md**

Under the existing "Flash it" section, add a "Flashing in a container (reproducible)"
subsection: prerequisite is podman (not host arduino-cli); `./flash.sh m5_echo` now
builds the toolchain image on first run and flashes through it; `compile` works on any
host including macOS; on macOS, *uploading* needs the QEMU `flasher` machine (link to
`docker/flash/README.md`). Keep the existing host-native instructions as the alternative
for hosts that already have arduino-cli (`FLASH_RUNTIME=native`).

- [ ] **Step 3: Verify docs commands match reality**

Run: `grep -n "FLASH_RUNTIME\|onju-flasher\|--group-add keep-groups\|PODMAN_CONNECTION" docker/flash/README.md docs/m5-getting-started.md`
Expected: the flags referenced in docs exactly match those in `flash.sh` (cross-check names).

- [ ] **Step 4: Commit**

```bash
git add docker/flash/README.md docs/m5-getting-started.md
git commit -m "docs(flash): containerized flashing flow + macOS QEMU appendix"
```

---

## Self-Review

**Spec coverage:**
- Reproducible pinned image → Task 1. ✓
- Rootless Linux `--device` + keep-groups → Task 4. ✓
- Host-side credential rendering (mac Keychain kept, Linux env/prompt) → Task 3. ✓
- Unchanged operator CLI surface → preserved in Tasks 2–4 (`compile`, `--regen`, explicit port). ✓
- macOS compile-anywhere + optional QEMU upload → Task 2 (compile under applehv) + Task 5 (QEMU appendix). ✓
- `FLASH_RUNTIME` native fallback → Tasks 2 & 4. ✓
- Version-lock reproducibility step → Task 1 Step 5. ✓
- Research encoded for revisit → lives in the design doc; referenced from Task 5. ✓

**Placeholder scan:** `<PID>` in Task 5 is a per-unit hardware value the operator reads from `lsusb`/`system_profiler` (documented how), not a plan gap. `ESP32_OPUS_VERSION` is intentionally resolved-then-locked in Task 1 Step 5 (concrete procedure, not "TBD").

**Type/name consistency:** `run_toolchain`, `ensure_image`, `detect_device`, and the vars `DEVICE_ARGS` / `CONTAINER_PORT` / `FLASH_RUNTIME` / `FLASH_IMAGE` / `FLASH_DRYRUN` / `FLASH_OS` / `PODMAN_CONNECTION` / `FLASH_SKIP_CREDS` are used identically across Tasks 2–5. Entrypoint action verbs (`compile|upload|flash|monitor`) match between the entrypoint (Task 1) and `run_toolchain` callers (Tasks 2–4).
