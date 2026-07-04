# ASR strategy: native parakeet daemon + Vulkan container bake-off

**Status:** DRAFT — pending peer review, then user approval before implementation.
**Date:** 2026-07-03 · **Repo:** onju-v2 (branch `integration`)

## Context / why

Today the ASR service (`pipeline/services/asr_server.py`, parakeet-mlx) runs as a bare
foreground process on the Mac and **dies when the launching session ends** — it's the
component that keeps going down. We also want to compare ASR engines/hosts for
latency/accuracy. Research settled two facts:

- **parakeet-mlx cannot be containerized.** MLX talks to **Metal directly**; macOS
  containers are Linux VMs whose only GPU interface is **Vulkan** (libkrun paravirt:
  Vulkan→Venus→MoltenVK→Metal). MLX has no Vulkan/Linux path. So parakeet is **native-only**.
- **whisper.cpp has a Vulkan backend**, so it *can* run GPU-accelerated in a container on
  **both** the Mac (libkrun machine) and lunarBeacon (AMD Radeon iGPU, `/dev/dri`).

So the clean split: **parakeet = native, service-managed daemon**; **whisper-vulkan =
GPU container**. Both speak the same `/transcribe` contract, selected by `asr.url`, and
benchmarked head-to-head.

## The ASR contract (already exists — do not change)

Reference impl: `pipeline/services/asr_server.py`; client: `pipeline/services/asr.py`.
- `POST /transcribe` — multipart field `audio` = WAV (16 kHz mono int16) →
  `{"text": str, "duration_s": float, "transcribe_time_s": float}` (`no_speech_prob` optional).
- `GET /health` → `{"status": "ok"|"loading", "model": str}`.
Every candidate implements exactly this (thin FastAPI wrapper per engine).

---

## Plan A — parakeet-mlx as a managed native daemon (`onju-asr`)

macOS has **no systemd**; the native manager is **launchd**. Deliver a LaunchAgent +
a wrapper CLI presenting systemctl-style verbs.

### Files (new)
- `deploy/mac/com.onju.asr.parakeet.plist.template` — LaunchAgent template with
  `__PYTHON__`, `__REPO__`, `__MODEL__`, `__HOST__`, `__PORT__`, `__DYLD__`, `__LOG__` placeholders.
- `deploy/mac/onju-asr` — wrapper CLI (bash). Verbs:
  `install | uninstall | start | stop | restart | status | logs`.
- `deploy/mac/README.md` — usage.

### LaunchAgent plist essentials
- `ProgramArguments`: `<repo>/.venv/bin/python -m pipeline.services.asr_server --port <port>`
- `EnvironmentVariables`: **`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`** (opus fix —
  launchd does NOT inherit shell env, this is mandatory), `ASR_MODEL`, `PATH`.
- `WorkingDirectory`: repo root. `RunAtLoad: true`. `KeepAlive: true` (crash + boot restart).
- `StandardOutPath`/`StandardErrorPath`: `~/Library/Logs/onju-asr.log`.
- Label: `com.onju.asr.parakeet`. Installed to `~/Library/LaunchAgents/`.

### Wrapper CLI behavior (modern launchctl, per-GUI domain)
- `install`: render template → `~/Library/LaunchAgents/com.onju.asr.parakeet.plist` with
  absolute paths resolved from the repo it's run in; `launchctl bootstrap gui/$(id -u) <plist>`.
- `start`/`stop`: `launchctl kickstart -k gui/$(id -u)/com.onju.asr.parakeet` /
  `launchctl bootout gui/$(id -u)/com.onju.asr.parakeet`.
- `restart`: `kickstart -k`. `status`: `launchctl print …` summary **+ `curl -s localhost:<port>/health`**.
- `logs`: `tail -f ~/Library/Logs/onju-asr.log`. `uninstall`: bootout + rm plist.

### Verify
`onju-asr install && onju-asr start && onju-asr status` → health `ok`; `kill` the pid →
KeepAlive restarts it; reboot → RunAtLoad brings it up; `pipeline.main` transcribes via it.

---

## Plan B — whisper-vulkan GPU container + bake-off + gateway container

### B1. Shared `/transcribe` adapter + whisper-vulkan image
Files (new, under `docker/asr/`):
- `adapter.py` — FastAPI implementing the contract, calling an injected engine fn
  `transcribe(wav_path) -> (text, duration_s)`; times it → `transcribe_time_s`. Engine-agnostic.
- `whisper-vulkan/Containerfile` — build whisper.cpp with `-DGGML_VULKAN=1` (Mesa/Vulkan
  runtime), + Python/FastAPI + `adapter.py` bound to a whisper.cpp invocation. Model baked or
  mounted. `ENV WHISPER_MODEL` (e.g. `ggml-base.en` / `small`).
- `whisper-vulkan/README.md`.

Run:
- **lunarBeacon (AMD):** `podman run -d --restart unless-stopped --device /dev/dri
  -p 8101:8101 onju-whisper-vulkan` → `asr.url: http://<lb>:8101`.
- **Mac (Metal via libkrun):** prerequisite `brew install krunkit` +
  `CONTAINERS_MACHINE_PROVIDER=libkrun podman machine init onju-gpu` (separate from the
  current `applehv` machine); then same `--device /dev/dri` run. **Documented, not automated**
  (provider switch is deliberate + version-sensitive per research).

Optional third candidate (nearly free): `whisper-cpu` = same adapter, no `--device`, CPU
build — useful as a bake-off floor. Mark optional.

### B2. Bake-off harness — `tests/bench_asr.py`
- Input: a dir of WAV clips + a `references.txt` (clip → expected transcript). **Best set =
  real captured M5 utterances** (record a handful via the live pipeline first).
- For each candidate URL in a small config list: POST every clip to `/transcribe`, collect
  `transcribe_time_s`, wall RTT, and text.
- Report per candidate: p50/p95 latency, mean RTT, and **WER** vs references
  (simple Levenshtein-over-words). Table output.

### B3. Gateway container (lunarBeacon)
- `docker/gateway/Containerfile` — base + `libopus`+`ffmpeg` + `uv pip install -e .`
  (reuse the `docker/flash` pattern). No DYLD needed on Linux.
- Run `--network host` (multicast discovery + dialing M5 TCP:3001, like the existing
  `nginx-tls-proxy`), `--restart unless-stopped`, mount `config.yaml` + `.env`.
- Replaces the current orphaned `pipeline.main` process (pid 2250884, unmanaged).

### Config selection
`asr.url` remains the single selector (no code change to swap). Bake-off runs candidates on
distinct ports; flip `asr.url` to promote the winner. Optionally add a named-candidate list
to config for convenience.

---

## Phasing / order
1. **Plan A** (`onju-asr` daemon) — fixes the down-component problem, self-contained. FIRST.
2. **B1** whisper-vulkan image + adapter → run on lunarBeacon (AMD) first (no provider switch).
3. **B2** bench harness + capture real M5 clips → run the bake-off.
4. **B1 on Mac** (libkrun machine) as an additional candidate.
5. **B3** gateway container.

## Risks / open questions (for review)
- **whisper.cpp Vulkan on the AMD iGPU** — will Mesa/Vulkan actually offload on that specific
  Radeon, or fall back to CPU? Needs an early `vulkaninfo`/smoke test before committing.
- **libkrun on this Mac** — provider switch + krunkit version sensitivity (podman 5.x breakage
  reported). Mac-GPU candidate may be flaky; lunarBeacon AMD is the primary GPU target.
- **launchctl domain nuances** — `gui/$(id -u)` vs `bootstrap` edge cases across macOS versions;
  wrapper must fail clearly, not silently.
- **File placement** — `deploy/mac/` vs repo-root `onju-asr` (discoverability like `flash.sh`).
- **Model choice / accuracy floor** — which whisper model size balances latency vs WER for
  short PTT commands; the bench answers this but pick a sane default.
- **Repo hygiene** — this lands on `integration`; PR #1 (`integration → master`) is open.
