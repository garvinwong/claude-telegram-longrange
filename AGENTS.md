# AGENTS.md — for coding agents working on this repo

## Layout
- `src/` — the daemon. Modules:
  - `config.py` — all config (env `TGLR_*` > `~/.claude-telegram-longrange/config.json`). Single source of truth.
  - `tasks.py` — SQLite task ledger (`TaskStore`). Survives restarts.
  - `runner.py` — spawns `claude -p --session-id/--resume --output-format stream-json --permission-mode default`; parses events; killpg on cancel/timeout; global timer table for `/extend`. Never adds `--dangerously-skip-permissions`.
  - `session_lock.py` — cross-process session mutex: TG-side `flock` (hard) + PC-side `.pc` heartbeat (soft, TTL). `LOCK_DIR` overridable via `TGLR_LOCK_DIR`.
  - `daemon.py` — `TgApi` (only place touching `requests`) + `Daemon` (routing, worker pool, pickers, command menu, `_pc_busy` guard + force-takeover).
  - `approval_relay.py` — polls the agents-island bridge, relays approvals to Telegram inline keyboards. Degrades to reading/writing the hook's queue/response files if the bridge is down. `watched_provider` unions `/watch`ed sessions into the active filter.
  - `progress.py` — one throttled progress card per task; final answer posted separately.
- `hooks/` — `session_lock_hook.sh`: PC-side heartbeat hook (SessionStart/PostToolUse/Stop refresh, SessionEnd release). Best-effort, zero stdout. Wire into `~/.claude/settings.json` (see README).
- `tests/` — pytest, everything mocked (no network, no real `claude`). `conftest.py` puts `src/` on the path and isolates `session_lock.LOCK_DIR` to a tmp dir (autouse).
- `launch/` — systemd `--user` unit template + `install.sh`.

## Conventions
- Comments are in Chinese (author's style); keep that if editing.
- Constructors take injected `api`/`store`/`cfg` so tests can mock. Keep this — do not hardcode globals into methods.
- Callback prefixes: `tglr:` (approvals, owned by relay), `sess:`/`tsel:`/`mdl:`/`sesspg:`/`taskpg:`/`force:` (pickers + force-takeover, owned by daemon). Whitelist check runs before routing.

## Run / test
```bash
pip install requests pytest
python3 -m pytest tests/ -v          # must stay green
TGLR_BOT_TOKEN=... TGLR_CHAT_ID=... TGLR_WORKDIR=... python3 src/daemon.py
```

## Hard rules
- Only ONE process may long-poll a given bot token (Telegram returns 409 otherwise). Before starting a second instance, stop the first.
- Long-task path must never use `--dangerously-skip-permissions`.
- Any string that may contain the bot token must pass through `config.redact()` before logging.
