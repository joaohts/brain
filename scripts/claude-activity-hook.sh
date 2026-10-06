#!/usr/bin/env bash
# claude-activity-hook.sh — Claude Code hook: record a session's activity.
#
# Registered in ~/.claude/settings.json for every event Claude does work on
# (README, "Inactivity timeout"), so it covers every Claude on the host.
# Each event is appended to
#   $CLAUDE_SESSIONS_STATE_DIR/activity/<session_id>.log
# as  ms TAB event TAB tool_use_id|type TAB transcript TAB pid TAB start TAB tmux
# where pid/start identify the claude process (start = /proc starttime, so a
# reused pid never matches) and tmux is its tmux session, if any.
# Stop and SessionStart start the file over: nothing is in flight then.
# Notification idle_prompt (the 60 s "waiting for input" nudge) is not
# activity. Never blocks or fails Claude: always exits 0.

EVENTS="SessionStart UserPromptSubmit PreToolUse PostToolUse PostToolUseFailure
PermissionRequest Notification Stop StopFailure SubagentStart SubagentStop
PreCompact PostCompact Elicitation ElicitationResult"

if [ "${1:-}" = "install" ] || [ "${1:-}" = "uninstall" ]; then
  # Add (or remove) this hook for every event in a settings file, keeping
  # everything else; a backup is left next to it. Usage:
  #   claude-activity-hook.sh install [~/.claude/settings.json]
  set -euo pipefail
  settings="${2:-$HOME/.claude/settings.json}"
  self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  [ -f "$settings" ] || echo '{}' > "$settings"
  cp "$settings" "$settings.bak-activity-$(date +%s)"
  tmp=$(mktemp "$settings.XXXXXX")
  jq --arg cmd "$self" --arg mode "$1" --arg events "$EVENTS" '
    .hooks = (.hooks // {}) |
    reduce ($events | split("\\s+"; null) | map(select(. != ""))[]) as $e (.;
      .hooks[$e] = ([(.hooks[$e] // [])[]
                     | .hooks = [.hooks[] | select(.command != $cmd)]
                     | select(.hooks | length > 0)]
                    + (if $mode == "install" then
                         [{hooks: [{type: "command", command: $cmd, timeout: 5}]}]
                       else [] end))
      | if (.hooks[$e] | length) == 0 then del(.hooks[$e]) else . end)
  ' "$settings" > "$tmp" && mv "$tmp" "$settings"
  echo "$1ed activity hook in $settings for: $(echo $EVENTS)"
  exit 0
fi

command -v jq >/dev/null 2>&1 || exit 0
input=$(cat) || exit 0
# fields joined by \x1f: a tab separator would collapse empty fields
IFS=$'\x1f' read -r event sid detail ntype transcript < <(
  printf '%s' "$input" | jq -r '[.hook_event_name, .session_id, .tool_use_id,
    .notification_type, .transcript_path] | map(. // "" | tostring
    | gsub("[\t\n\u001f]"; " ")) | join("\u001f")' 2>/dev/null) || exit 0
case "$event" in
  ""|SessionEnd) exit 0 ;;
  Notification)
    case "$ntype" in idle_prompt|"") exit 0 ;; esac
    detail="$ntype" ;;
esac
case "$sid" in ""|*/*|.*) exit 0 ;; esac

# the claude process: nearest ancestor named claude
pid=$PPID start=""
for _ in 1 2 3 4 5 6; do
  [ -r "/proc/$pid/stat" ] || { pid=""; break; }
  stat=$(cat "/proc/$pid/stat" 2>/dev/null) || { pid=""; break; }
  rest=${stat##*) }                       # fields after "(comm)"
  comm=${stat#*(}; comm=${comm%)*}
  if [ "$comm" = "claude" ]; then
    set -- $rest; start=${20}; break     # field 22 = starttime
  fi
  set -- $rest; pid=$2                    # field 4 = ppid
  [ "$pid" -gt 1 ] 2>/dev/null || { pid=""; break; }
done
[ -n "$start" ] || pid=""

tmux_session=""
if [ -n "${TMUX_PANE:-}" ]; then
  tmux_session=$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}' 2>/dev/null)
fi

dir="${CLAUDE_SESSIONS_STATE_DIR:-$HOME/.local/state/claude-sessions}/activity"
mkdir -p "$dir" 2>/dev/null || exit 0
line="$(date +%s%3N)"$'\t'"$event"$'\t'"$detail"$'\t'"$transcript"$'\t'"$pid"$'\t'"$start"$'\t'"$tmux_session"
case "$event" in
  Stop|SessionStart) printf '%s\n' "$line" > "$dir/$sid.log" ;;
  *)                 printf '%s\n' "$line" >> "$dir/$sid.log" ;;
esac 2>/dev/null
exit 0
