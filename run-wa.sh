#!/bin/bash
# Entry point for the WhatsApp sidecar (deploy/systemd/brain-wa.service.in).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
eval "$(.venv/bin/python -m brain.config --shell whatsapp)"
if [ "$WA_ENABLED" != "1" ]; then
  echo "whatsapp is disabled in config.toml ([whatsapp] enabled = false)" >&2
  exit 1
fi
[ -d "$HOME/.local/node/current/bin" ] && export PATH="$HOME/.local/node/current/bin:$PATH"
cd wa
exec node index.js
