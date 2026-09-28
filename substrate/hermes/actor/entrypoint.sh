#!/bin/sh
# Writes Hermes' config from the template's env, starts the gateway and warms it
# up (see warmup.py).
# Runs once per golden snapshot: every agent is restored from that snapshot,
# so nothing here runs again on wake.
set -eu

: "${API_SERVER_KEY:?API_SERVER_KEY must be set (at least 16 characters)}"
export MODEL_NAME="${MODEL_NAME:-google/gemma-4-12B-it}"
export MODEL_BASE_URL="${MODEL_BASE_URL:?MODEL_BASE_URL must be set, e.g. http://proxy:8091/llm/v1}"
export MAX_TOKENS="${MAX_TOKENS:-50}"
export CONTEXT_LENGTH="${CONTEXT_LENGTH:-65536}"

mkdir -p "$HERMES_HOME"
python3 - <<'EOF'
import os, string
src = open("/opt/keynote/config.yaml.tmpl").read()
dst = os.path.join(os.environ["HERMES_HOME"], "config.yaml")
open(dst, "w").write(string.Template(src).substitute(os.environ))
EOF
[ -f /data/AGENTS.md ] || cp /opt/keynote/AGENTS.md /data/AGENTS.md

# The gateway runs as a child; warmup.py warms it up, answers the template's
# readyz on :8081/ready once warm, and exits if the gateway does.
hermes gateway run &
exec python3 /opt/keynote/warmup.py "$!"
