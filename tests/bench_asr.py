#!/usr/bin/env python3
"""
ASR bake-off harness.

Sends a fixed set of WAV clips to each candidate /transcribe endpoint,
reports per-candidate latency (p50/p95) and normalised WER.

Usage:
    # run from the gateway host (lunarBeacon) to reflect production RTT
    python tests/bench_asr.py tests/bench_clips/ \
        --candidates parakeet=http://192.168.100.23:8100 whisper=http://localhost:8101

Clip directory layout:
    bench_clips/
        clip_001.wav
        clip_002.wav
        ...
        references.txt      # one line per clip: "<filename> <reference transcript>"
        # e.g.:  clip_001.wav what is the capital of france

References file format:
    <wav_filename> <reference text>
    clip_001.wav what is the capital of france
    clip_002.wav tell me a short joke
"""

import argparse
import re
import statistics
import sys
import time
from pathlib import Path

import httpx


# ── WER ──────────────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _wer(reference: str, hypothesis: str) -> float:
    r = _normalise(reference).split()
    h = _normalise(hypothesis).split()
    if not r:
        return 0.0
    # Levenshtein over word sequences
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            if r[i - 1] == h[j - 1]:
                d[i][j] = d[i - 1][j - 1]
            else:
                d[i][j] = 1 + min(d[i - 1][j], d[i][j - 1], d[i - 1][j - 1])
    return d[len(r)][len(h)] / len(r)


# ── helpers ──────────────────────────────────────────────────────────────────

def _wait_healthy(name: str, url: str, timeout: int = 60) -> bool:
    base = url.rstrip("/").rsplit("/", 1)[0] if url.endswith("/transcribe") else url.rstrip("/")
    health_url = base + "/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(health_url, timeout=5)
            if r.status_code == 200 and r.json().get("status") == "ok":
                print(f"  [{name}] healthy ({r.json().get('model', '?')})")
                return True
        except Exception:
            pass
        time.sleep(2)
    print(f"  [{name}] NOT healthy after {timeout}s — skipping", file=sys.stderr)
    return False


def _transcribe(url: str, wav_path: Path) -> tuple[str, float, float]:
    """Return (text, transcribe_time_s, wall_rtt_s). Raises on HTTP error."""
    base = url.rstrip("/")
    endpoint = base if base.endswith("/transcribe") else base + "/transcribe"
    with open(wav_path, "rb") as f:
        t0 = time.perf_counter()
        resp = httpx.post(
            endpoint,
            files={"audio": (wav_path.name, f, "audio/wav")},
            timeout=60,
        )
        rtt = time.perf_counter() - t0
    resp.raise_for_status()
    j = resp.json()
    return j["text"], j["transcribe_time_s"], rtt


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ASR bake-off harness")
    parser.add_argument("clips_dir", help="Directory with WAV clips + references.txt")
    parser.add_argument(
        "--candidates", nargs="+", metavar="NAME=URL",
        default=["parakeet=http://192.168.100.23:8100",
                 "whisper-vulkan=http://localhost:8101"],
        help="Candidate endpoints as name=url pairs",
    )
    parser.add_argument(
        "--warmup", type=int, default=1,
        help="Number of warmup requests to discard per candidate (default: 1)",
    )
    parser.add_argument(
        "--health-timeout", type=int, default=60,
        help="Seconds to wait for /health before skipping a candidate",
    )
    args = parser.parse_args()

    clips_dir = Path(args.clips_dir)
    refs_file = clips_dir / "references.txt"
    if not refs_file.exists():
        print(f"ERROR: {refs_file} not found", file=sys.stderr)
        sys.exit(1)

    references: dict[str, str] = {}
    for line in refs_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            references[parts[0]] = parts[1]

    clips = sorted(p for p in clips_dir.glob("*.wav") if p.name in references)
    if not clips:
        print(f"ERROR: no WAV files found in {clips_dir} matching references.txt", file=sys.stderr)
        sys.exit(1)

    print(f"\nBench set: {len(clips)} clips in {clips_dir}")
    print(f"Candidates: {len(args.candidates)}")
    print(f"Warmup requests: {args.warmup}\n")

    candidates = {}
    for item in args.candidates:
        name, _, url = item.partition("=")
        candidates[name] = url

    results: dict[str, dict] = {}

    for name, url in candidates.items():
        print(f"── {name} @ {url} ──")
        if not _wait_healthy(name, url, args.health_timeout):
            continue

        # Warmup: discard first N requests (RADV compiles shaders on first inference)
        if args.warmup and clips:
            print(f"  warming up ({args.warmup}x) ...", end="", flush=True)
            for _ in range(args.warmup):
                try:
                    _transcribe(url, clips[0])
                except Exception as e:
                    print(f" WARN: warmup failed: {e}", flush=True)
                    break
            print(" done")

        transcribe_times = []
        wall_rtts = []
        wers = []
        errors = 0

        for wav in clips:
            ref = references[wav.name]
            try:
                text, t_svc, t_rtt = _transcribe(url, wav)
                transcribe_times.append(t_svc)
                wall_rtts.append(t_rtt)
                w = _wer(ref, text)
                wers.append(w)
                print(f"  {wav.name}: {t_svc:.3f}s svc / {t_rtt:.3f}s rtt  WER={w:.2f}"
                      f"  hyp={repr(text[:60])}")
            except Exception as e:
                print(f"  {wav.name}: ERROR {e}", file=sys.stderr)
                errors += 1

        if transcribe_times:
            results[name] = {
                "t_svc_p50": statistics.median(transcribe_times),
                "t_svc_p95": sorted(transcribe_times)[int(len(transcribe_times) * 0.95)],
                "t_rtt_p50": statistics.median(wall_rtts),
                "t_rtt_p95": sorted(wall_rtts)[int(len(wall_rtts) * 0.95)],
                "wer_mean":  sum(wers) / len(wers) if wers else float("nan"),
                "n": len(transcribe_times),
                "errors": errors,
                "url": url,
            }
        print()

    if not results:
        print("No results — all candidates failed or were unreachable.")
        sys.exit(1)

    # ── Summary table ────────────────────────────────────────────────────────
    cols = ["candidate", "n", "svc_p50", "svc_p95", "rtt_p50", "rtt_p95", "WER_mean", "errs"]
    rows = []
    for name, r in results.items():
        rows.append([
            name,
            r["n"],
            f"{r['t_svc_p50']:.3f}s",
            f"{r['t_svc_p95']:.3f}s",
            f"{r['t_rtt_p50']:.3f}s",
            f"{r['t_rtt_p95']:.3f}s",
            f"{r['wer_mean']:.3f}",
            r["errors"],
        ])

    print("=" * 72)
    print("RESULTS")
    print(f"NOTE: svc=transcribe_time_s (engine), rtt=wall time (includes network).")
    print(f"      WER is normalised (lowercase, no punctuation). Lower is better.")
    print(f"      Placement: run from gateway host to reflect production latency.")
    print("=" * 72)
    widths = [max(len(str(r[i])) for r in [cols] + rows) for i in range(len(cols))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*cols))
    print("-" * sum(widths + [2] * (len(cols) - 1)))
    for row in rows:
        print(fmt.format(*row))
    print("=" * 72)


if __name__ == "__main__":
    main()
