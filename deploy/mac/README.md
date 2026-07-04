# onju-asr — parakeet ASR LaunchAgent for macOS

Manages `pipeline.services.asr_server` (parakeet-mlx) as a macOS LaunchAgent:
auto-starts at login, restarts on crash, binds to the LAN interface so the
lunarBeacon gateway can reach it.

## Prerequisites

```bash
# from the repo root
uv venv
uv pip install -e ".[asr]" "numba>=0.61" "llvmlite>=0.44"
brew install opus   # or: brew install libopus
```

## Quick start

```bash
# From the repo root — onju-asr discovers the repo automatically
./deploy/mac/onju-asr install   # renders plist, bootstraps LaunchAgent
./deploy/mac/onju-asr status    # prints launchctl state + /health response
./deploy/mac/onju-asr logs      # tail live log
```

The first startup downloads the model (~1 GB) from HuggingFace — allow ~30–60 s.

## Commands

| Command | Effect |
|---|---|
| `install` | Render plist with absolute paths → `~/Library/LaunchAgents/`, bootstrap |
| `uninstall` | Bootout + remove plist |
| `start` | `kickstart -k` (kill-and-restart) |
| `stop` | `launchctl stop` (process stops; service stays registered; `start` resumes it) |
| `restart` | `kickstart -k` |
| `status` | launchctl print + `/health` curl |
| `logs` | `tail -f ~/Library/Logs/onju-asr.log` |

## Configuration overrides

Set these env vars before running `install` to customise:

| Variable | Default | Notes |
|---|---|---|
| `ASR_HOST` | auto-detected LAN IP (`en7` then `en0`) | Set to the IP lunarBeacon can reach |
| `ASR_PORT` | `8100` | Must match `asr.url` in lunarBeacon `config.yaml` |
| `ASR_MODEL` | `mlx-community/parakeet-tdt-0.6b-v3` | Any parakeet-mlx compatible model |
| `DYLD_FALLBACK_LIBRARY_PATH` | `/opt/homebrew/lib` | Must contain `libopus.dylib` |

Example: use a different LAN IP and model

```bash
ASR_HOST=192.168.100.23 ASR_MODEL=mlx-community/parakeet-tdt-1.1b-v2 \
  ./deploy/mac/onju-asr install
```

## How it works

`onju-asr install` renders `com.onju.asr.parakeet.plist.template` → a concrete
plist with **absolute paths** (launchd does not expand `~`), then bootstraps it
into the `gui/<uid>` LaunchAgent domain.

`KeepAlive: true` means launchd restarts the process on crash. To stop it temporarily, run `onju-asr stop` (`launchctl stop` — the service stays registered
so `start` can resume it). `KeepAlive` will restart a crashed process but not a cleanly
stopped one. To remove it entirely, use `onju-asr uninstall`.

`DYLD_FALLBACK_LIBRARY_PATH` is set explicitly in the plist `EnvironmentVariables`
because launchd does not inherit your shell's environment. The startup log prints
the resolved value so you can verify it survived SIP:

```text
2026-07-03 12:00:00 INFO parakeet: DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
```

## Verify crash-restart

```bash
# Get the pid
launchctl print gui/$(id -u)/com.onju.asr.parakeet | grep pid
# Kill it — launchd should restart within seconds
kill <pid>
sleep 5
./deploy/mac/onju-asr status
```

## Update after a code change

Re-install to pick up plist changes; a plain `restart` suffices for code-only changes:

```bash
./deploy/mac/onju-asr install   # re-renders + re-bootstraps (idempotent)
# or, if only Python code changed:
./deploy/mac/onju-asr restart
```
