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

# Upload action with an explicit device: must map it and add keep-groups.
out2="$(FLASH_DRYRUN=1 FLASH_SKIP_CREDS=1 FLASH_OS=linux \
        ./flash.sh m5_echo /dev/ttyUSB0)"
echo "$out2" | grep -q -- "--device /dev/ttyUSB0" || fail "device not mapped on upload"
echo "$out2" | grep -q -- "--group-add keep-groups" || fail "keep-groups missing"
echo "$out2" | grep -Eq "onju-flasher +m5_echo +flash +/dev/ttyUSB0" || fail "upload args wrong"
echo "PASS: flash.sh upload dry-run maps device rootless"
