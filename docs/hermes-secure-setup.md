# Secure Hermes setup for onju-voice

This pipeline sends transcribed speech to a [hermes-agent](https://github.com/NickJLange/hermes-agent)
API server, which runs the agent's tool loop and streams a spoken reply back.
Two facts make security non-optional:

1. **Hermes' API server exposes the agent's full toolset — including the host
   terminal.** The default `hermes-api-server` platform toolset keeps `terminal`,
   `file`, and `code_execution`.
2. **Voice can't answer an approval prompt.** Transcription is error-prone, and a
   misheard sentence must never be able to run a destructive command. Hermes'
   interactive approval prompt is useless here because there's no one at a keyboard.

So the security model is **capability restriction first** (the voice channel
literally doesn't have dangerous tools), with approval auto-deny as defense in
depth — not the other way around.

`./setup_hermes.sh` (run from the repo root) configures the loopback-bound API
server with a bearer key and prints the snippets below. This doc is the full
reference.

---

## 1. Enable the API server (loopback + bearer)

In `~/.hermes/.env` on the Hermes host:

```bash
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1        # loopback only — never 0.0.0.0; expose via a tunnel/proxy (§4)
API_SERVER_PORT=8642
API_SERVER_KEY=<48-char random>  # required for every deployment, even loopback
```

`chmod 600 ~/.hermes/.env`. On the pipeline side, export the same value as
`HERMES_API_SERVER_KEY` and reference it from `pipeline/config.yaml`
(`hermes.api_key: "${HERMES_API_SERVER_KEY}"`).

## 2. Restrict the voice channel's toolset

`platform_toolsets` narrows tools **per platform**, so this affects only the
`api_server` channel — your interactive CLI, Telegram, etc. keep full
capabilities. No separate profile required. In the running profile's
`~/.hermes/config.yaml`:

```yaml
platform_toolsets:
  api_server:
    - safe            # web_search, web_extract, vision, image_gen — read-only, no fs/terminal/code
    - memory          # persistent cross-session memory
    - skills          # browse + use skills
    - session_search  # recall past conversations
    # optional and still voice-safe: homeassistant (smart-home control), todo, clarify
    # deliberately ABSENT: terminal, file, code_execution, browser, delegation, cronjob
```

The `safe` core toolset is purpose-built for this: "read-only research + media
generation. No file writes, no terminal, no code execution."

### Verify the restriction actually took effect

After starting the gateway:

```bash
curl -s http://127.0.0.1:8642/v1/toolsets \
  -H "Authorization: Bearer $API_SERVER_KEY" | grep -iE 'terminal|execute_code|write_file'
# Expected: no output. If any of these print, the restriction did not apply.
```

This check is part of the verification gate — treat a non-empty result as a
hard failure.

## 3. Defense in depth: approvals

Even with terminal excluded, set auto-deny so any future toolset change can't
silently open a hole:

```yaml
approvals:
  mode: smart        # LLM risk-assessment: auto-deny genuinely dangerous commands
  cron_mode: deny    # headless cron can't auto-approve dangerous commands
```

Hermes' always-on **hardline blocklist** (rm -rf /, fork bombs, disk wipes)
applies regardless and can't be overridden.

## 4. Reaching Hermes across networks

The pipeline → Hermes link should not assume the two hosts share a LAN. Keep the
API server on `127.0.0.1` and put an authenticated, encrypted path in front of
it. Two options:

### Tailscale (recommended)

- Install Tailscale on both the Hermes host and the Mac Mini (pipeline).
- The pipeline reaches Hermes at its tailnet address:
  `hermes.base_url: "http://<hermes-tailscale-ip>:8642/v1"`.
- No public port, identity enforced at the network layer **and** the bearer key,
  near-zero latency, no certificate management. Optionally use Tailscale `serve`
  to present `https://` with an automatic cert.

### Caddy reverse proxy + real cert (public domain)

If you need a public hostname instead of a tailnet:

```caddyfile
hermes.example.com {
    reverse_proxy 127.0.0.1:8642
}
```

Caddy auto-provisions TLS. The bearer key still gates every request; keep
`API_SERVER_CORS_ORIGINS` unset (the pipeline calls server-to-server). Point the
pipeline at `https://hermes.example.com/v1`.

### nginx reverse proxy + source-IP allowlist (LAN / Docker host-networking)

Useful when Hermes runs in a container with `network_mode: host` (e.g. the
upstream `docker-compose.yml`) and you want a LAN-facing endpoint that only a
known host (the pipeline) may reach. Two things are **mandatory** here, both a
consequence of host networking and of SSE:

1. **Bind Hermes to loopback, not `0.0.0.0`.** With host networking, a Hermes
   port on `0.0.0.0` is already reachable on the LAN, so anyone can bypass the
   proxy. Set `API_SERVER_HOST=127.0.0.1` in `~/.hermes/.env` (which maps to
   `/opt/data/.env` inside the container) so the proxy is the only LAN-facing
   door. Run nginx on the host (or a host-networked nginx container) so it can
   reach `127.0.0.1:8642`.
2. **Disable proxy buffering, or streaming breaks.** nginx buffers proxied
   responses by default, which holds the entire SSE stream until the turn ends —
   the response then arrives all at once and looks like a hang. The streaming
   pipeline depends on token-by-token delivery, so buffering must be off.

```nginx
server {
    listen 192.168.100.10:8643;     # LAN-facing port (or `443 ssl` with a cert for the real endpoint)
    server_name hermes.lan.example;

    # Source-IP allowlist — tighten to the pipeline host's /32, not the whole subnet.
    allow 192.168.100.20/32;   # replace with your pipeline host's actual IP
    deny  all;

    location /v1/ {
        proxy_pass http://127.0.0.1:8642;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # Authorization is forwarded by default; Hermes still enforces the bearer key.

        # --- SSE: must NOT buffer or token streaming is lost ---
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header Connection '';
        chunked_transfer_encoding off;
        proxy_read_timeout 300s;     # agentic turns run tools for 60s+
    }
}
```

Caveats: the source-IP allowlist is **defense-in-depth, not authentication** —
LAN source IPs are spoofable on a flat segment, so the bearer key remains the real
gate. And a pinned allowlist **conflicts with the roaming goal**: it locks out the
pipeline (or a device) the moment it leaves the allowed subnet, which is one
reason roaming is deferred to the Tailscale-overlay phase. For a fixed home LAN
it's a reasonable surface-reduction layer. Add `listen 443 ssl` with a cert to
turn this same block into the WAN-capable authenticated HTTPS endpoint.

Either way the pipeline config is just:

```yaml
conversation:
  backend: "hermes"
  hermes:
    base_url: "https://<tunnel-or-domain>/v1"
    api_key: "${HERMES_API_SERVER_KEY}"
```

## 5. Residual risk to know about

Enabling `safe`/web tools means the agent can fetch web content, which is an
avenue for prompt injection. With no terminal, file-write, or code execution in
the toolset, the blast radius is bounded to what the *safe* tools can do (search,
read pages, analyze/generate images, touch memory). Hermes' SSRF protection stays
on by default, blocking the agent's web tools from reaching private/loopback
addresses. If you later add `homeassistant`, understand that voice can then
actuate smart-home devices.

## Testing profile (optional)

A dedicated profile is **not** needed for security (per-platform restriction
handles that inside your main profile). It's only useful to isolate test runs:

```bash
hermes profile create onju
cat >> ~/.hermes/profiles/onju/.env <<'EOF'
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8643
API_SERVER_KEY=<test-key>
EOF
# apply the same platform_toolsets restriction in that profile's config.yaml
hermes -p onju gateway
```

Point the pipeline at port 8643 while testing, then switch back to your main
profile's API server when you're satisfied.
