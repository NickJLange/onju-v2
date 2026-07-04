#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $1" >&2; exit 1; }
trap 'rm -f m5_echo/credentials.h' EXIT

rm -f m5_echo/credentials.h
# Force the non-macOS path and supply creds via env; dry-run skips podman.
out="$(FLASH_OS=linux WIFI_SSID=testnet WIFI_PASSWORD=secret123 \
       FLASH_DRYRUN=1 ./flash.sh m5_echo compile)"
[ -f m5_echo/credentials.h ] || fail "credentials.h not rendered"
grep -q 'testnet' m5_echo/credentials.h || fail "SSID not substituted"
grep -q 'secret123' m5_echo/credentials.h || fail "password not substituted"
echo "PASS: linux credential rendering"
