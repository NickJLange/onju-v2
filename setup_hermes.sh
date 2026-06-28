#!/usr/bin/env bash
# Enable the hermes-agent API server for onju-voice, bound to loopback with a
# bearer key. Prints (but does NOT silently apply) the restricted-toolset config
# so the voice channel can't reach the host terminal. See
# docs/hermes-secure-setup.md for the full secure deployment recipe.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"

if [ ! -d "$HERMES_HOME" ]; then
    echo "==> Error: $HERMES_HOME not found. Is hermes-agent installed?"
    echo "    Install it first (https://hermes-agent.nousresearch.com/), then re-run."
    exit 1
fi

touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

set_env() {
    # set_env KEY VALUE — idempotent upsert into the .env file.
    local key="$1" val="$2"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        # Leave an existing value in place (don't clobber a key the user set).
        echo "==> $key already set in $ENV_FILE — leaving as-is."
    else
        echo "${key}=${val}" >> "$ENV_FILE"
        echo "==> Set $key in $ENV_FILE."
    fi
}

# Generate a strong key only if one isn't already configured.
if grep -qE "^API_SERVER_KEY=" "$ENV_FILE"; then
    API_KEY="$(grep -E "^API_SERVER_KEY=" "$ENV_FILE" | head -1 | cut -d= -f2-)"
    echo "==> Reusing existing API_SERVER_KEY."
else
    API_KEY="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 48)"
    echo "API_SERVER_KEY=${API_KEY}" >> "$ENV_FILE"
    echo "==> Generated a new API_SERVER_KEY."
fi

set_env API_SERVER_ENABLED true
set_env API_SERVER_HOST 127.0.0.1   # loopback only; expose via a tunnel/proxy, not 0.0.0.0
set_env API_SERVER_PORT 8642

cat <<EOF

==> API server is configured (loopback, bearer auth).

    On the pipeline (Mac Mini) side, export the matching key:

        export HERMES_API_SERVER_KEY='${API_KEY}'

    and set conversation.backend: "hermes" in pipeline/config.yaml with
        hermes.base_url: "http://127.0.0.1:8642/v1"   (local)
      or a tunneled https URL for a remote Hermes host.

==> SECURITY — restrict the voice channel's toolset. Voice transcripts are
    error-prone and can't answer approval prompts, so the api_server platform
    must NOT reach the host terminal. Add this to the running profile's
    $HERMES_HOME/config.yaml (this only narrows the api_server platform —
    your CLI/Telegram keep full tools):

        platform_toolsets:
          api_server:
            - safe            # web_search, web_extract, vision, image_gen (read-only, no fs/terminal)
            - memory          # persistent cross-session memory
            - skills          # browse + use skills
            - session_search  # recall past conversations
            # optional, still safe for voice: homeassistant, todo, clarify
            # deliberately NOT: terminal, file, code_execution, browser, delegation, cronjob

        approvals:
          mode: smart         # auto-deny dangerous commands as defense-in-depth
          cron_mode: deny

    Then start the gateway and VERIFY the restriction took effect:

        hermes gateway
        curl -s http://127.0.0.1:8642/v1/toolsets \\
          -H "Authorization: Bearer \$API_SERVER_KEY" | grep -i terminal
        # ^ should print nothing — terminal must be absent.

==> Done. Full guide: docs/hermes-secure-setup.md
EOF
