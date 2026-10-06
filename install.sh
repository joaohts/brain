#!/usr/bin/env bash
# install.sh — set up the brain in this directory. Idempotent: safe to rerun.
#
# Touches only this repo directory and ~/.config/systemd/user. Never
# overwrites an existing config.toml, .env, data/identity.md or wa/allow.json.
#
#   ./install.sh              venv + deps, seed config files, render user units
#   ./install.sh --no-units   skip the systemd units (e.g. for a test install)
#   ./install.sh --enable     also enable + start brain (and brain-wa if
#                             [whatsapp] enabled = true in config.toml)
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=1 ENABLE=0
for arg in "$@"; do
  case "$arg" in
    --no-units) UNITS=0 ;;
    --enable)   ENABLE=1 ;;
    -h|--help)  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
[ "$UNITS" = 0 ] && [ "$ENABLE" = 1 ] && { echo "--enable needs the units (drop --no-units)" >&2; exit 2; }

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

cd "$DIR"

# -- prerequisites -------------------------------------------------------------
say "checking prerequisites"
have python3 || die "python3 not found"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "python 3.11+ required (found $(python3 -V 2>&1))"
python3 -c 'import venv, ensurepip' 2>/dev/null \
  || die "python3 venv support missing (Debian/Ubuntu: apt install python3-venv)"
[ -d "$HOME/.local/node/current/bin" ] && PATH="$HOME/.local/node/current/bin:$PATH"
NODE_OK=0
if have node && have npm; then
  if [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 20 ]; then
    NODE_OK=1
  else
    warn "node $(node --version) is too old (WhatsApp needs 20+): the WhatsApp sidecar (wa/) will not be installed"
  fi
else
  warn "node/npm not found: the WhatsApp sidecar (wa/) will not be installed"
fi
for opt in comms claude tmux jq; do
  have "$opt" || warn "optional: '$opt' not found (needed only by the comms / claude_sessions integrations)"
done

# -- python --------------------------------------------------------------------
say "python venv + requirements"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

# -- node (WhatsApp sidecar) ---------------------------------------------------
if [ "$NODE_OK" = 1 ]; then
  say "npm ci in wa/"
  (cd wa && npm ci --silent --no-audit --no-fund)
fi

# -- config files (never overwritten) -----------------------------------------
seed() {  # seed <example> <target> [mode]
  if [ -e "$2" ]; then
    echo "    keep  $2"
  else
    mkdir -p "$(dirname "$2")"
    cp "$1" "$2"
    [ -n "${3:-}" ] && chmod "$3" "$2"
    echo "    new   $2  (edit it)"
  fi
}
say "config files"
mkdir -p data/memory
seed config.example.toml config.toml
seed .env.example .env 600
seed examples/identity.example.md data/identity.md
seed wa/allow.example.json wa/allow.json 600
.venv/bin/python -m brain.config || warn "config.toml does not validate yet — fix it before starting"

# -- systemd user units --------------------------------------------------------
if [ "$UNITS" = 1 ]; then
  say "systemd user units -> $UNIT_DIR"
  mkdir -p "$UNIT_DIR"
  for tpl in deploy/systemd/*.service.in; do
    unit="$(basename "${tpl%.in}")"
    sed "s|@INSTALL_DIR@|$DIR|g" "$tpl" > "$UNIT_DIR/$unit.tmp"
    if cmp -s "$UNIT_DIR/$unit.tmp" "$UNIT_DIR/$unit"; then
      rm "$UNIT_DIR/$unit.tmp"; echo "    same  $unit"
    else
      mv "$UNIT_DIR/$unit.tmp" "$UNIT_DIR/$unit"; echo "    wrote $unit"
    fi
  done
  if have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
  else
    warn "systemctl --user unavailable here; run 'systemctl --user daemon-reload' in a login session"
  fi
fi

# -- enable (opt-in) -----------------------------------------------------------
if [ "$ENABLE" = 1 ]; then
  grep -q '^OPENAI_API_KEY=.\+' .env || die ".env has no OPENAI_API_KEY; set it, then rerun with --enable"
  .venv/bin/python -m brain.config >/dev/null || die "config.toml is invalid; not enabling"
  say "enabling brain.service"
  systemctl --user enable --now brain.service
  if eval "$(.venv/bin/python -m brain.config --shell whatsapp)" && [ "$WA_ENABLED" = 1 ]; then
    [ "$NODE_OK" = 1 ] || die "whatsapp is enabled but node/npm are missing"
    say "enabling brain-wa.service (scan the QR: tail -f data/wa.log)"
    systemctl --user enable --now brain-wa.service
  fi
  have loginctl && { loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes \
    || warn "run 'sudo loginctl enable-linger $USER' so the services start at boot without a login"; }
fi

say "done. Next: edit config.toml, .env and data/identity.md$([ "$ENABLE" = 1 ] || echo ", then ./install.sh --enable")"
