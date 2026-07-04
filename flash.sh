#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
FLASH_OS="$(printf '%s' "${FLASH_OS:-$(uname -s)}" | tr '[:upper:]' '[:lower:]')"  # darwin | linux

# -------------------------------------------------------
# Target config
# -------------------------------------------------------
TARGET="${1:-onjuino}"
shift 2>/dev/null || true

case "$TARGET" in
    onjuino)
        PROJECT_DIR="$REPO/onjuino"
        ;;
    m5_echo|m5echo)
        PROJECT_DIR="$REPO/m5_echo"
        ;;
    --*|compile*)
        # No target specified, treat as flag — default to onjuino
        set -- "$TARGET" "$@"
        TARGET="onjuino"
        PROJECT_DIR="$REPO/onjuino"
        ;;
    *)
        echo "Unknown target: $TARGET (expected onjuino or m5_echo)"
        exit 1
        ;;
esac

TEMPLATE="$PROJECT_DIR/credentials.h.template"
OUTPUT="$PROJECT_DIR/credentials.h"

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

# run_toolchain <action> [container_port]
# Container port is the device path AS SEEN IN THE CONTAINER (set by Task 4).
run_toolchain() {
    local action="$1" cport="${2:-}"
    if [ "$FLASH_RUNTIME" = "native" ]; then
        FLASH_WORKDIR="$REPO" "$REPO/docker/flash/entrypoint.sh" "$TARGET" "$action" "$cport"
        return
    fi
    local tty=""; [ -t 0 ] && tty="-it"
    # DEVICE_ARGS is populated by Task 4 for upload/monitor; empty for compile.
    local cmd=(podman)
    [ -n "${PODMAN_CONNECTION:-}" ] && cmd+=(--connection "$PODMAN_CONNECTION")
    # ${DEVICE_ARGS:-} is intentionally word-split (unquoted) so it can expand to
    # multiple podman args. The device-mapping task must keep device paths
    # space-free (or switch DEVICE_ARGS to an array) to avoid breakage here.
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
NO_MONITOR=false
PORT=""

for arg in "$@"; do
    case "$arg" in
        compile|compile-only) COMPILE_ONLY=true ;;
        --regen) REGEN=true ;;
        --no-monitor) NO_MONITOR=true ;;
        -h|--help)
            echo "Usage: flash.sh [target] [options] [port]"
            echo ""
            echo "Targets: onjuino (default), m5_echo"
            echo ""
            echo "Options:"
            echo "  compile          Compile only, no upload"
            echo "  --regen          Force regenerate WiFi credentials"
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
    WIFI_SSID="${WIFI_SSID:-}"
    WIFI_PASSWORD="${WIFI_PASSWORD:-}"

    if [ "$FLASH_OS" = "darwin" ]; then
        # ---- macOS Keychain + networksetup discovery ----
        WIFI_IF=$(networksetup -listallhardwareports 2>/dev/null | awk '/Wi-Fi/{getline; print $2}')
        WIFI_IF="${WIFI_IF:-en0}"

        _SSID_RAW=$(networksetup -getairportnetwork "$WIFI_IF" 2>/dev/null | sed 's/Current Wi-Fi Network: //')
        if [ -z "$_SSID_RAW" ] || [[ "$_SSID_RAW" == *"not associated"* ]] || [[ "$_SSID_RAW" == *"not a Wi-Fi"* ]] || [[ "$_SSID_RAW" == *"Error"* ]]; then
            _SSID_RAW=""
        fi
        [ -n "$_SSID_RAW" ] && WIFI_SSID="${WIFI_SSID:-$_SSID_RAW}"

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

        if [ -z "$WIFI_PASSWORD" ]; then
            echo "Retrieving WiFi password from Keychain (Touch ID may be required)..."
            WIFI_PASSWORD=$(security find-generic-password -wa "$WIFI_SSID" 2>/dev/null || true)
            if [ -z "$WIFI_PASSWORD" ]; then
                echo "Could not retrieve password for '$WIFI_SSID' from Keychain."
                read -sp "WiFi password: " WIFI_PASSWORD
                echo ""
            fi
        fi
    fi

    # ---- cross-platform fallback / Linux path ----
    [ -z "$WIFI_SSID" ] && [ -t 0 ] && read -rp "WiFi SSID: " WIFI_SSID
    [ -z "$WIFI_SSID" ] && { echo "ERROR: No WiFi SSID (set WIFI_SSID)"; exit 1; }
    if [ -z "$WIFI_PASSWORD" ] && [ -t 0 ]; then
        read -rsp "WiFi password: " WIFI_PASSWORD; echo ""
    fi
    [ -z "$WIFI_PASSWORD" ] && { echo "ERROR: No WiFi password (set WIFI_PASSWORD)"; exit 1; }

    echo "WiFi SSID: $WIFI_SSID"
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

DEVICE_ARGS=""
CONTAINER_PORT=""
detect_device "$PORT"

echo ""
echo "Flashing $TARGET..."
run_toolchain flash "$CONTAINER_PORT"

if [ "$NO_MONITOR" != true ]; then
    echo "Starting serial monitor..."
    run_toolchain monitor "$CONTAINER_PORT"
fi
