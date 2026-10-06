"""Config for the brain. Everything personal or per-host lives in config.toml
(git-ignored; start from config.example.toml) and secrets live in .env.

load() returns one flat dict: the [agent], [model], [limits] and [server]
keys at top level, and each integration as a nested dict
(cfg["whatsapp"], cfg["comms"], cfg["calendar"], cfg["claude_sessions"],
cfg["vault"]).
Relative paths resolve against the repo root. An enabled integration with a
broken setting raises ConfigError at startup rather than failing mid-turn.

    python -m brain.config --check           validate config.toml
    python -m brain.config --shell whatsapp  print WA_* exports for run-wa.sh
"""

import os
import pathlib
import shlex
import sys
import tomllib

BASE = pathlib.Path(__file__).parent
REPO = BASE.parent
CONFIG_FILE = pathlib.Path(os.environ.get("BRAIN_CONFIG", REPO / "config.toml"))

DEFAULTS = {
    # [agent]
    "assistant_name": "Assistant",
    "owner_name": "Owner",
    "language": "English",
    "timezone": "UTC",
    "identity_file": "data/identity.md",
    "memory_dir": "data/memory",
    "db_path": "data/brain.db",
    # [model]
    "model": "gpt-6-luna",
    "price_in_per_mtok": 0.10,    # USD per 1M input tokens
    "price_out_per_mtok": 0.50,   # USD per 1M output tokens
    "reasoning_effort": "medium",
    "daily_budget_usd": 1.0,      # 0 disables the cap; local midnight
    "weekly_budget_usd": 0.0,     # 0 disables the cap; Monday 00:00 local
    "budget_alert_channel": "",   # 80%/100% alerts; "" = owner's WhatsApp, if any
    "budget_cli_max_daily_usd": 0.0,    # highest temporary cap the agent CLI
    "budget_cli_max_weekly_usd": 0.0,   # may set; 0 = the CLI can't raise it
    # [limits]
    "max_tool_steps": 6,
    "window_turns": 20,           # tool-trace rows share the window with real turns
    "auto_compact_turns": 40,     # thread longer than this -> compact older half
    "blackboard_hours": 4,
    "blackboard_max_lines": 12,
    # [server]
    "host": "127.0.0.1",
    "http_port": 3401,
}

INTEGRATIONS = {
    "whatsapp": {
        "enabled": False,
        "port": 3402,
        "contacts_file": "wa/allow.json",
        "auth_dir": "wa/auth",
        "transcribe_model": "whisper-1",
        "vision_model": "gpt-5-mini",
        "transcribe_language": "",
        "owner_channel": "cli",
    },
    "comms": {
        "enabled": False,
        "socket": "~/.local/share/comms/node.sock",
        "alias": "brain",
        "trusted_machines": [],
        "state_path": "data/comms-v1/adapter.db",
        "queue_count": 32,
        "queue_bytes": 4 * 1024 * 1024,
    },
    "calendar": {
        "enabled": False,
        "script": "",
    },
    "claude_sessions": {
        "enabled": False,
        "script": "scripts/claude-sessions.sh",
        "source": "brain",
        "idle_minutes": 480,    # reap a worker after this long inactive; 0 = reap at [FINAL]
        "reap_scope": "brain",  # whose sessions time out: brain | managed | all
        "state_dir": "~/.local/state/claude-sessions",  # activity logs (hook)
    },
    "vault": {
        "enabled": False,
        "path": "",                 # notes folder (e.g. an Obsidian vault)
        "read_only": [],            # folder prefixes readable but never written
        "deny": [],                 # globs (vault-relative) never read or written
        "max_note_bytes": 1_000_000,   # larger notes are refused
        "max_read_chars": 20_000,      # per vault_read call; page with offset
        "max_write_bytes": 200_000,    # largest note a write may produce
        "max_results": 50,             # list entries / search hits per call
        "search_seconds": 5,
    },
}

# Fixed replies sent without a model call. Write them in [agent] language.
MESSAGES = {
    "budget_reached": "I've reached my {period} spending limit (US$ {limit}). "
                      "Your messages are saved and I'll answer them when it "
                      "resets ({resets}). {owner} can raise it for now with "
                      "\"budget today N\" or \"budget week N\".",
    "out_of_steps": "I ran out of steps before finishing this: {request}",
    "failure": "Something failed on my side before I could answer: {request}",
}

SECTIONS = ("agent", "model", "limits", "server")
PATH_KEYS = ("identity_file", "memory_dir", "db_path")
INTEGRATION_PATHS = {"whatsapp": ("contacts_file", "auth_dir"),
                     "comms": ("socket", "state_path"),
                     "calendar": ("script",),
                     "claude_sessions": ("script",),
                     "vault": ("path",)}


class ConfigError(Exception):
    pass


def _path(p: str) -> str:
    if not p:
        return p
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else str(REPO / p)


def load(path=None) -> dict:
    path = pathlib.Path(path) if path else CONFIG_FILE
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    cfg = dict(DEFAULTS)
    for section in SECTIONS:
        for k, v in raw.get(section, {}).items():
            if k == "name" and section == "model":
                k = "model"
            elif k == "port" and section == "server":
                k = "http_port"
            if k not in DEFAULTS:
                raise ConfigError(f"[{section}] unknown key: {k}")
            cfg[k] = v
    for name, defaults in INTEGRATIONS.items():
        section = dict(defaults)
        for k, v in raw.get(name, {}).items():
            if k not in defaults:
                raise ConfigError(f"[{name}] unknown key: {k}")
            section[k] = v
        for k in INTEGRATION_PATHS[name]:
            section[k] = _path(section[k])
        cfg[name] = section
    cfg["messages"] = dict(MESSAGES)
    for k, v in raw.get("messages", {}).items():
        if k not in MESSAGES:
            raise ConfigError(f"[messages] unknown key: {k}")
        cfg["messages"][k] = v
    unknown = set(raw) - set(SECTIONS) - set(INTEGRATIONS) - {"messages"}
    if unknown:
        raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    for k in PATH_KEYS:
        cfg[k] = _path(cfg[k])
    validate(cfg)
    return cfg


def message(cfg, key: str, **values) -> str:
    """A configured fixed reply with {owner}/{request} (budget_reached also
    {period}/{limit}/{spent}/{resets}) filled in; a placeholder the caller
    has no value for is left empty."""
    class Blank(dict):
        def __missing__(self, k):
            return ""
    values.setdefault("owner", cfg["owner_name"])
    return cfg["messages"][key].format_map(Blank(values))


def enabled(cfg, name: str) -> bool:
    return bool(cfg.get(name, {}).get("enabled"))


def validate(cfg) -> None:
    """Fail loud for enabled-but-broken integrations; disabled ones are inert."""
    import re
    if not 0 < cfg["window_turns"] < cfg["auto_compact_turns"]:
        raise ConfigError("[limits] window_turns must be > 0 and smaller than "
                          "auto_compact_turns (otherwise every turn compacts)")
    for k in ("daily_budget_usd", "weekly_budget_usd",
              "budget_cli_max_daily_usd", "budget_cli_max_weekly_usd"):
        if not isinstance(cfg[k], (int, float)) or cfg[k] < 0:
            raise ConfigError(f"[model] {k} must be a number >= 0 (0 = no cap)")
    if enabled(cfg, "whatsapp"):
        f = cfg["whatsapp"]["contacts_file"]
        if not os.path.isfile(f):
            raise ConfigError(f"whatsapp: contacts_file not found: {f} "
                              f"(copy wa/allow.example.json)")
        oc = str(cfg["whatsapp"]["owner_channel"])
        # logout notices can't go over the channel that just logged out
        if not (oc == "cli" or oc.startswith(("comms-v1:", "comms:"))):
            raise ConfigError(f"whatsapp: owner_channel must be 'cli' or a "
                              f"comms-v1:/comms: channel, got {oc!r}")
        if oc.startswith(("comms-v1:", "comms:")) and not enabled(cfg, "comms"):
            raise ConfigError(f"whatsapp: owner_channel {oc!r} needs [comms] enabled")
    if enabled(cfg, "comms"):
        c = cfg["comms"]
        if not isinstance(c["trusted_machines"], list) or any(
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(m))
                for m in c["trusted_machines"]):
            raise ConfigError("comms: trusted_machines must be a list of machine IDs")
        if not os.path.exists(c["socket"]):
            raise ConfigError(f"comms: node socket not found: {c['socket']} "
                              f"(is the comms node running?)")
    if enabled(cfg, "claude_sessions") and not enabled(cfg, "comms"):
        raise ConfigError("claude_sessions needs [comms] enabled: workers "
                          "receive tasks and report back over comms")
    if enabled(cfg, "vault"):
        v = cfg["vault"]
        if not v["path"] or not os.path.isdir(v["path"]):
            raise ConfigError(f"vault: path is not a folder: {v['path']!r}")
        for k in ("max_note_bytes", "max_read_chars", "max_write_bytes",
                  "max_results", "search_seconds"):
            if not isinstance(v[k], (int, float)) or v[k] <= 0:
                raise ConfigError(f"vault: {k} must be a positive number")
        for k in ("read_only", "deny"):
            if not isinstance(v[k], list) or not all(isinstance(x, str) for x in v[k]):
                raise ConfigError(f"vault: {k} must be a list of strings")
    for name in ("calendar", "claude_sessions"):
        if enabled(cfg, name):
            s = cfg[name]["script"]
            if not s or not os.access(s, os.X_OK):
                raise ConfigError(f"{name}: script missing or not executable: {s!r}")


def api_key() -> str:
    k = os.environ.get("OPENAI_API_KEY")
    if k:
        return k
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("no OPENAI_API_KEY in env or repo .env")


def _shell(cfg, name: str) -> str:
    if name != "whatsapp":
        raise SystemExit(f"--shell supports: whatsapp")
    w = cfg["whatsapp"]
    pairs = {"WA_ENABLED": "1" if w["enabled"] else "0",
             "WA_PORT": w["port"],
             "WA_CONTACTS": w["contacts_file"],
             "WA_AUTH": w["auth_dir"],
             "WA_TRANSCRIBE_MODEL": w["transcribe_model"],
             "WA_VISION_MODEL": w["vision_model"],
             "WA_TRANSCRIBE_LANGUAGE": w["transcribe_language"],
             "WA_LANGUAGE": cfg["language"],
             "WA_OWNER_CHANNEL": w["owner_channel"],
             "BRAIN_URL": f"http://{cfg['host']}:{cfg['http_port']}"}
    return "\n".join(f"export {k}={shlex.quote(str(v))}" for k, v in pairs.items())


if __name__ == "__main__":
    try:
        cfg = load()
    except (ConfigError, tomllib.TOMLDecodeError) as e:
        sys.exit(f"config error: {e}")
    if sys.argv[1:2] == ["--shell"] and len(sys.argv) == 3:
        print(_shell(cfg, sys.argv[2]))
    else:
        on = [n for n in INTEGRATIONS if enabled(cfg, n)] or ["none"]
        print(f"config ok: model={cfg['model']} port={cfg['http_port']} "
              f"integrations={','.join(on)}")
