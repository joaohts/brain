#!/usr/bin/env bash
# claude-sessions.sh — manage tmux-backed `claude` sessions on this host.
#
# Subcommands: create | list | kill | info
# Each session = a tmux session running `claude` in a chosen cwd with full perms.
# Requires: tmux, the comms CLI (`comms claude`), Claude Code, and the
# /open-comms skill that the comms installer provides; jq is optional.
# The Remote Control bridge auto-registers it, so the session appears in your
# claude.ai app under Code → Sessions and is controllable from there.

set -euo pipefail

LOG_DIR="${CLAUDE_SESSIONS_LOG_DIR:-$HOME/.local/state/claude-sessions}"
mkdir -p "$LOG_DIR"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") create --cwd <dir> [--source <name>]
  $(basename "$0") list [--json]
  $(basename "$0") kill <session-id>
  $(basename "$0") info <session-id>

Sources name who started the session (e.g. brain, manual).
EOF
}

# ---------- helpers ----------

new_id() {
  local source="$1"
  local rand
  rand=$(head -c 4 /dev/urandom | xxd -p)
  echo "${source}-${rand}"
}

is_managed() {
  local name="$1"
  tmux show-environment -t "$name" CLAUDE_SESSION_SOURCE 2>/dev/null \
    | grep -q '^CLAUDE_SESSION_SOURCE='
}

env_get() {
  local name="$1" var="$2"
  tmux show-environment -t "$name" "$var" 2>/dev/null | sed "s/^${var}=//"
}

claude_alive() {
  # Foreground process in the pane. Bash/sh = claude has exited.
  local name="$1"
  local cmd
  cmd=$(tmux list-panes -t "$name" -F '#{pane_current_command}' 2>/dev/null | head -1)
  case "$cmd" in
    bash|sh|zsh|fish|"") return 1 ;;
    *) return 0 ;;
  esac
}

pre_trust_cwd() {
  # Mark the cwd as trusted in ~/.claude.json so claude does not show the
  # workspace-trust dialog at startup (which would block remote control).
  local cwd="$1"
  local config="$HOME/.claude.json"
  [ ! -f "$config" ] && return 0
  command -v jq >/dev/null || return 0
  local tmp
  tmp=$(mktemp "${config}.XXXXXX") || return 0
  if jq --arg cwd "$cwd" \
        '.projects[$cwd] = ((.projects[$cwd] // {}) + {hasTrustDialogAccepted: true})' \
        "$config" > "$tmp" 2>/dev/null; then
    mv "$tmp" "$config"
  else
    rm -f "$tmp"
  fi
}

fetch_control_url() {
  # Poll the tmux pane for claude's "/remote-control is active" line
  # and extract the https://claude.ai/code/session_... URL. Up to ~30s (the
  # banner can take >12s to render on slow hosts; a short window returned null
  # even though Remote Control was up).
  local id="$1"
  local i=0 url=""
  while [ $i -lt 60 ]; do
    url=$(tmux capture-pane -p -t "$id" 2>/dev/null \
          | grep -oE 'https://claude\.ai/code/session_[A-Za-z0-9_-]+' \
          | head -1)
    [ -n "$url" ] && { echo "$url"; return 0; }
    sleep 0.5
    i=$((i+1))
  done
  return 1
}

wait_repl_ready() {
  # Poll the pane until the Claude REPL is interactive (ready for input).
  # Decoupled from the control-URL scrape, which can lag past its window on slow
  # hosts even after the REPL is up. The bypass-permissions footer renders once
  # the prompt is interactive and stays visible.
  local id="$1"
  local i=0 dev_confirmed=0
  while [ $i -lt 60 ]; do   # up to ~30s
    local pane
    pane=$(tmux capture-pane -p -t "$id" 2>/dev/null || true)
    if [ "$dev_confirmed" -eq 0 ] &&
       printf '%s\n' "$pane" | grep -qE '^[[:space:]]*(WARNING:[[:space:]]*)?Loading development channels[[:space:]]*$' &&
       printf '%s\n' "$pane" | grep -qE '^[[:space:]]*Channels:[[:space:]]*server:comms[[:space:]]*$' &&
       [ "$(printf '%s\n' "$pane" | grep -oE '(server|plugin):[^[:space:]]+' | tr '\n' ' ')" = 'server:comms ' ] &&
       printf '%s\n' "$pane" | grep -q 'Enter to confirm'; then
      # comms claude loads the local development MCP channel. Confirm its
      # standard Claude prompt before waiting for the REPL, otherwise the
      # auto-open-comms step below can be consumed by this prompt.
      tmux send-keys -t "$id" Enter
      dev_confirmed=1
      # Let Claude finish consuming the confirmation before the launcher
      # submits the first slash command to the now-live REPL.
      sleep 1
    fi
    # A footer can remain visible behind a confirmation dialog. Do not treat
    # that as an interactive REPL; only the ordinary footer without a pending
    # confirmation is ready for the auto-open command.
    if printf '%s\n' "$pane" | grep -qE 'bypass permissions on|shift\+tab to cycle' &&
       ! printf '%s\n' "$pane" | grep -q 'Enter to confirm'; then
      return 0
    fi
    sleep 0.5
    i=$((i+1))
  done
  return 1
}

# ---------- commands ----------

cmd_create() {
  local cwd="" source="manual"
  while [ $# -gt 0 ]; do
    case "$1" in
      --cwd) cwd="$2"; shift 2 ;;
      --source) source="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
  done
  [ -z "$cwd" ] && { echo "--cwd is required" >&2; exit 2; }
  [ -d "$cwd" ] || { echo "cwd not found: $cwd" >&2; exit 2; }

  source=$(echo "$source" | tr -c 'a-zA-Z0-9-' '-' | sed 's/-\+/-/g; s/^-//; s/-$//')
  [ -z "$source" ] && source="manual"

  local id log_path created_at
  id=$(new_id "$source")
  log_path="$LOG_DIR/$id.log"
  created_at=$(date -Iseconds)

  pre_trust_cwd "$cwd"

  tmux new-session -d -s "$id" -c "$cwd" -x 220 -y 50
  tmux set-environment -t "$id" CLAUDE_SESSION_SOURCE "$source"
  tmux set-environment -t "$id" CLAUDE_SESSION_CWD "$cwd"
  tmux set-environment -t "$id" CLAUDE_SESSION_CREATED_AT "$created_at"
  tmux pipe-pane -t "$id" "cat >> '$log_path'"

  tmux send-keys -t "$id" \
    "COMMS_ALIAS='session-$id' comms claude --remote-control --dangerously-skip-permissions --remote-control-session-name-prefix '$source'" Enter

  local control_url=""
  control_url=$(fetch_control_url "$id" || true)

  local repl_ready=""
  wait_repl_ready "$id" && repl_ready=1

  local url_field='null'
  [ -n "$control_url" ] && url_field="\"$control_url\""

  # Auto-join inter-agent comms: open the attachment + arm the receiving stream.
  # MUST run inside the session (send-keys) so the stream is a harness-owned
  # background task the SESSION owns — only such a task can wake an idle model.
  # A pre-launch/external process, detached shell or daemon cannot.
  # Gated on REPL-ready (not control_url): a lagging URL scrape no longer skips
  # the join. Alias session-$id -> auto-qualifies to <host>:session-$id.
  # --global is explicit: the node never implies remote scope on a new
  # attachment, and these sessions must reach peers on other machines.
  if [ -n "$repl_ready" ]; then
    # The footer can render just before the REPL accepts carriage return.
    # Give the interactive prompt a short settling window before submitting
    # the supported slash command.
    sleep 2
    tmux send-keys -t "$id" "/open-comms session-$id --global" Enter
  fi

  cat <<EOF
{
  "id": "$id",
  "source": "$source",
  "cwd": "$cwd",
  "created_at": "$created_at",
  "log_path": "$log_path",
  "control_url": $url_field,
  "note": "Session auto-appears in your claude.ai app under Code → Sessions (remote control bridge)."
}
EOF
}

cmd_list() {
  local json="false"
  [ "${1:-}" = "--json" ] && json="true"

  local rows="" first=1
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    is_managed "$name" || continue
    local source cwd created_at alive log_path
    source=$(env_get "$name" CLAUDE_SESSION_SOURCE)
    cwd=$(env_get "$name" CLAUDE_SESSION_CWD)
    created_at=$(env_get "$name" CLAUDE_SESSION_CREATED_AT)
    log_path="$LOG_DIR/$name.log"
    if claude_alive "$name"; then alive="true"; else alive="false"; fi

    if [ "$json" = "true" ]; then
      [ $first -eq 1 ] || rows="${rows},"
      first=0
      rows="${rows}{\"id\":\"$name\",\"source\":\"$source\",\"cwd\":\"$cwd\",\"created_at\":\"$created_at\",\"alive\":$alive,\"log_path\":\"$log_path\"}"
    else
      printf "%-26s %-10s %-6s %-25s %s\n" "$name" "$source" "$alive" "$created_at" "$cwd"
    fi
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)

  if [ "$json" = "true" ]; then
    echo "[$rows]"
  fi
}

cmd_kill() {
  local id="${1:-}"
  [ -z "$id" ] && { echo "session id required" >&2; exit 2; }
  tmux has-session -t "$id" 2>/dev/null || { echo "no such session: $id" >&2; exit 1; }
  is_managed "$id" || { echo "refusing to kill unmanaged tmux session: $id" >&2; exit 1; }

  # Graceful: send Ctrl-C, wait up to 3s for claude to flush its transcript.
  tmux send-keys -t "$id" C-c
  local i=0
  while [ $i -lt 6 ]; do
    claude_alive "$id" || break
    sleep 0.5
    i=$((i+1))
  done

  tmux kill-session -t "$id" 2>/dev/null || true
  echo "{\"id\":\"$id\",\"killed\":true}"
}

cmd_info() {
  local id="${1:-}"
  [ -z "$id" ] && { echo "session id required" >&2; exit 2; }
  tmux has-session -t "$id" 2>/dev/null || { echo "no such session: $id" >&2; exit 1; }
  is_managed "$id" || { echo "not a managed session: $id" >&2; exit 1; }

  local source cwd created_at alive log_path
  source=$(env_get "$id" CLAUDE_SESSION_SOURCE)
  cwd=$(env_get "$id" CLAUDE_SESSION_CWD)
  created_at=$(env_get "$id" CLAUDE_SESSION_CREATED_AT)
  log_path="$LOG_DIR/$id.log"
  if claude_alive "$id"; then alive="true"; else alive="false"; fi

  cat <<EOF
{
  "id": "$id",
  "source": "$source",
  "cwd": "$cwd",
  "created_at": "$created_at",
  "alive": $alive,
  "log_path": "$log_path"
}
EOF
}

case "${1:-}" in
  create) shift; cmd_create "$@" ;;
  list)   shift; cmd_list "$@" ;;
  kill)   shift; cmd_kill "$@" ;;
  info)   shift; cmd_info "$@" ;;
  -h|--help|help|"") usage; exit 0 ;;
  *) echo "unknown subcommand: $1" >&2; usage; exit 2 ;;
esac
