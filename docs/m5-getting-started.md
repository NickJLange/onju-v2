# Getting started with the M5Stack ATOM Echo

This is the practical guide to bringing up the **M5Stack ATOM Echo** you bought
and getting it talking to the pipeline. It's a $13 ESP32-PICO-D4 dev kit with a
mic, a small speaker, one RGB LED, and a button — a cheap stand-in for the custom
Onjuino PCB. It uses **push-to-talk**: hold the button to speak, release to
listen. (Hardware/protocol internals live in [`../m5_echo/README.md`](../m5_echo/README.md);
this page is just how to use it.)

## What you need

- The ATOM Echo + its USB-C cable.
- A Mac (the flashing tooling uses macOS Keychain + `networksetup`).
- `arduino-cli` with the ESP32 core:
  ```bash
  brew install arduino-cli
  arduino-cli core install esp32:esp32
  ```
- The **pipeline server running on the same LAN** (see the main
  [README](../README.md)). Discovery is multicast and the server dials the device,
  so for now the Echo and the pipeline must be on the same network segment —
  roaming across networks is a documented future phase, not built yet.

## Flash it

From the repo root, with the Echo plugged in:

```bash
./flash.sh m5_echo
```

`flash.sh` does everything in one shot:

1. Installs the required Arduino libraries if missing (`Adafruit NeoPixel`,
   `esp32_opus`).
2. Generates `m5_echo/credentials.h` with your WiFi info — it auto-detects your
   current SSID, pulls the password from **Keychain** (Touch ID may prompt), and
   falls back to asking you if it can't. Re-run with `./flash.sh m5_echo --regen`
   to change networks.
3. Compiles, auto-detects the USB serial port (`/dev/cu.usbserial-*`), uploads,
   and opens the serial monitor.

Useful variants:

```bash
./flash.sh m5_echo compile          # compile only, no device needed
./flash.sh m5_echo /dev/cu.usbserial-XXXX   # force a specific port
./flash.sh m5_echo --no-monitor     # skip the serial monitor after upload
```

**If upload fails** (`Error: ... / Upload failed`): hold the **BOOT** button,
press **RESET**, release **BOOT**, then run the command again. The PICO-D4
sometimes needs a manual bootloader entry.

## Confirm it connected

After flashing, the serial monitor opens automatically (or run
`python serial_monitor.py` later — it auto-detects the port). On a healthy boot
you'll see it join WiFi, send its multicast announcement (tagged `PTT`), and the
pipeline open a TCP connection back. The pipeline log will show the device
appear. The RGB LED reflects state.

Quick local sanity checks over serial (115200 baud) — these don't need the
network:

| Key | What it does |
|-----|--------------|
| `P` | Play a local 440 Hz tone — proves the speaker / I2S path works |
| `T` | Raw mic test — prints sample values so you can confirm the mic |
| `M` / `m` | Force the mic on for 10s / off (no button needed) |
| `A` | Re-send the multicast announcement (re-trigger discovery) |
| `c` | Config mode — set SSID, password, **server address**, and volume |
| `r` | Reboot |

If the speaker tone (`P`) works but you get no agent audio, the issue is
network/discovery, not the device. Use `A` to re-announce, and check the device
and pipeline are on the same LAN.

## Talk to it

Hold the button, speak, release. Your speech is streamed to the pipeline (mu-law
over UDP), transcribed, sent to the Hermes agent, and the spoken reply comes back
(Opus over TCP) out the Echo's speaker. Because the Echo's speaker is small, the
pipeline uses a voice tuned for it by default (`default_voice_ptt` in
`config.yaml`).

### Pointing it at a specific server

Normally discovery is automatic on the LAN. If you need to pin the server (e.g.
multiple servers, or a non-default setup), enter config mode with `c` over serial
and set the server address there.

## Battery (optional)

The Echo runs off USB. For a cord-free unit, add the
[Atomic Battery Base](https://shop.m5stack.com/products/atomic-battery-base-200mah).

## Troubleshooting

- **No mic data / silence upstream:** the PICO-D4's PDM mic has board-specific
  quirks documented in [`../m5_echo/README.md`](../m5_echo/README.md#i2s-quirks-on-esp32-pico-d4).
  Confirm with `T` first.
- **Audio drops / buffer issues:** the Echo has **no PSRAM**, so it runs smaller
  buffers than the Onjuino. This is expected; keep it on solid WiFi.
- **Device never appears at the pipeline:** almost always a LAN/discovery problem
  — same segment? firewall blocking multicast/UDP 3000 / TCP 3001? Try `A`.
- **Wrong WiFi after moving:** `./flash.sh m5_echo --regen`, or set it live via
  config mode `c`.
