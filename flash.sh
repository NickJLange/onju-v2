#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"

# -------------------------------------------------------
# Target config
# -------------------------------------------------------
TARGET="${1:-onjuino}"
shift 2>/dev/null || true

case "$TARGET" in
    onjuino)
        FQBN="esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,UploadSpeed=115200"
        PROJECT_DIR="$REPO/onjuino"
        INO_NAME="onjuino.ino"
        PORT_GLOBS=("/dev/cu.usbmodem*")
        ;;
    m5_echo|m5echo)
        FQBN="esp32:esp32:m5stack_atom:UploadSpeed=1500000"
        PROJECT_DIR="$REPO/m5_echo"
        INO_NAME="m5_echo.ino"
        PORT_GLOBS=("/dev/cu.usbserial-*" "/dev/cu.usbmodem*")
        ;;
    --*|compile*)
        # No target specified, treat as flag — default to onjuino
        set -- "$TARGET" "$@"
        TARGET="onjuino"
        FQBN="esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,UploadSpeed=115200"
        PROJECT_DIR="$REPO/onjuino"
        INO_NAME="onjuino.ino"
        PORT_GLOBS=("/dev/cu.usbmodem*")
        ;;
    *)
        echo "Unknown target: $TARGET (expected onjuino or m5_echo)"
        exit 1
        ;;
esac

TEMPLATE="$PROJECT_DIR/credentials.h.template"
OUTPUT="$PROJECT_DIR/credentials.h"
BUILD_DIR="$PROJECT_DIR/build"

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

# -------------------------------------------------------
# Ensure dependencies (native mode only; container carries its own toolchain)
# -------------------------------------------------------
if [ "$FLASH_RUNTIME" = "native" ]; then
    REQUIRED_LIBS=("Adafruit NeoPixel" "esp32_opus")
    for lib in "${REQUIRED_LIBS[@]}"; do
        if ! arduino-cli lib list 2>/dev/null | grep -q "$lib"; then
            echo "Installing missing library: $lib"
            arduino-cli lib install "$lib"
        fi
    done
fi

# -------------------------------------------------------
# Flags
# -------------------------------------------------------
COMPILE_ONLY=false
REGEN=false
FORCE_COMPILE=false
NO_MONITOR=false
PORT=""

for arg in "$@"; do
    case "$arg" in
        compile|compile-only) COMPILE_ONLY=true ;;
        --regen) REGEN=true; FORCE_COMPILE=true ;;
        --force) FORCE_COMPILE=true ;;
        --no-monitor) NO_MONITOR=true ;;
        -h|--help)
            echo "Usage: flash.sh [target] [options] [port]"
            echo ""
            echo "Targets: onjuino (default), m5_echo"
            echo ""
            echo "Options:"
            echo "  compile          Compile only, no upload"
            echo "  --regen          Force regenerate WiFi credentials"
            echo "  --force          Force recompile even if unchanged"
            echo "  --no-monitor     Skip serial monitor after flash"
            echo "  /dev/...         Upload to specific port"
            exit 0 ;;
        /dev/*) PORT="$arg" ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

echo "Target: $TARGET"

# -------------------------------------------------------
# Generate credentials.h (only if missing or --regen)
# -------------------------------------------------------
if [ "${FLASH_SKIP_CREDS:-0}" != "1" ]; then
if [ -f "$OUTPUT" ] && [ "$REGEN" = false ]; then
    echo "Using existing credentials.h (pass --regen to regenerate)"
else
    WIFI_SSID=""

    WIFI_IF=$(networksetup -listallhardwareports 2>/dev/null | awk '/Wi-Fi/{getline; print $2}')
    WIFI_IF="${WIFI_IF:-en0}"

    WIFI_SSID=$(networksetup -getairportnetwork "$WIFI_IF" 2>/dev/null | sed 's/Current Wi-Fi Network: //')
    if [ -z "$WIFI_SSID" ] || [[ "$WIFI_SSID" == *"not associated"* ]] || [[ "$WIFI_SSID" == *"not a Wi-Fi"* ]] || [[ "$WIFI_SSID" == *"Error"* ]]; then
        WIFI_SSID=""
    fi

    if [ -z "$WIFI_SSID" ]; then
        PREFERRED=$(networksetup -listpreferredwirelessnetworks "$WIFI_IF" 2>/dev/null | tail -n +2 | sed 's/^[[:space:]]*//')
        if [ -n "$PREFERRED" ]; then
            TOP_SSID=$(echo "$PREFERRED" | head -1)
            echo "Known WiFi networks:"
            NETWORK_LIST=$(echo "$PREFERRED" | head -5)
            echo "$NETWORK_LIST" | cat -n
            NUM_NETWORKS=$(echo "$NETWORK_LIST" | wc -l | tr -d ' ')
            echo ""
            read -p "WiFi SSID [$TOP_SSID]: " WIFI_SSID
            if [ -z "$WIFI_SSID" ]; then
                WIFI_SSID="$TOP_SSID"
            elif [[ "$WIFI_SSID" =~ ^[0-9]+$ ]] && [ "$WIFI_SSID" -ge 1 ] && [ "$WIFI_SSID" -le "$NUM_NETWORKS" ]; then
                WIFI_SSID=$(echo "$NETWORK_LIST" | sed -n "${WIFI_SSID}p")
            fi
        fi
    fi

    [ -z "$WIFI_SSID" ] && read -p "WiFi SSID: " WIFI_SSID
    [ -z "$WIFI_SSID" ] && { echo "ERROR: No WiFi SSID provided."; exit 1; }
    echo "WiFi SSID: $WIFI_SSID"

    echo "Retrieving WiFi password from Keychain (Touch ID may be required)..."
    WIFI_PASSWORD=$(security find-generic-password -wa "$WIFI_SSID" 2>/dev/null || true)
    if [ -z "$WIFI_PASSWORD" ]; then
        echo "Could not retrieve password for '$WIFI_SSID' from Keychain."
        read -sp "WiFi password: " WIFI_PASSWORD
        echo ""
    fi
    [ -z "$WIFI_PASSWORD" ] && { echo "ERROR: No WiFi password provided."; exit 1; }

    sed -e "s|{{WIFI_SSID}}|${WIFI_SSID}|g" \
        -e "s|{{WIFI_PASSWORD}}|${WIFI_PASSWORD}|g" \
        "$TEMPLATE" > "$OUTPUT"
    echo "Generated credentials.h"
fi
fi  # FLASH_SKIP_CREDS
echo ""

# -------------------------------------------------------
# Generate git_hash.h (rewrite only on change to avoid recompile churn)
# -------------------------------------------------------
GIT_HASH=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "------")
GIT_HASH_FILE="$PROJECT_DIR/git_hash.h"
NEW_CONTENT="#define GIT_HASH \"${GIT_HASH}\""
if [ ! -f "$GIT_HASH_FILE" ] || [ "$(cat "$GIT_HASH_FILE")" != "$NEW_CONTENT" ]; then
    echo "$NEW_CONTENT" > "$GIT_HASH_FILE"
    echo "Updated git_hash.h to ${GIT_HASH}"
fi

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
