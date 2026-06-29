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
