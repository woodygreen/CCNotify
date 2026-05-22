# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

CCFeishuNotify is a Claude Code hook script that sends Feishu (Lark) interactive card notifications when Claude Code submits prompts, completes tasks, or requires user input/permission. It is invoked by Claude Code's hook system (UserPromptSubmit / Stop / Notification events) via stdin JSON.

The script is **single-file by design** — all logic lives in `src/ccfeishunotify.py`. There is no build step.

## Directory Layout (load-bearing)

```
src/ccfeishunotify.py           # main script
configs/feishu_config.json      # credentials (gitignored)
configs/feishu_config.example.json
db/ccfeishunotify.db            # SQLite session tracking
db/summary_queue.json           # AI summary queue
logs/ccfeishunotify.log         # daily-rotated log
```

The script auto-detects whether it lives under a `src/` subdirectory and resolves `configs/`, `db/`, `logs/` relative to the project root (`ClaudePromptTracker.__init__`). When invoked as a hook, the project root is `~/.claude/ccfeishunotify/`. **Do not hardcode paths or assume cwd** — use `self.project_dir`.

## Deployment Model

- The repo at `D:\MT\ccnotify` is the source of truth.
- The script is deployed by **copying** `src/`, `configs/`, `db/`, `logs/` into `~/.claude/ccfeishunotify/` (Windows: `C:\Users\Administrator\.claude\ccfeishunotify\`).
- Hooks in `~/.claude/settings.json` invoke `python "<dest>\src\ccfeishunotify.py" <EventName>`.
- After editing `src/ccfeishunotify.py`, copy it to the deploy target to take effect:
  ```bash
  cp src/ccfeishunotify.py ~/.claude/ccfeishunotify/src/
  ```
- Verify with `python ~/.claude/ccfeishunotify/src/ccfeishunotify.py` — should print `ok`.

## Architecture

The script entry point is `main()`, which dispatches by `sys.argv[1]`:
- `UserPromptSubmit` → `handle_user_prompt_submit` — inserts a row into the SQLite `prompt` table; an SQLite trigger auto-increments `seq` per session.
- `Stop` → `handle_stop` — finds the latest open prompt for the session, computes duration, parses the Claude transcript JSONL (`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`) for tokens / files / commands / decisions, and sends a green "completed" card.
- `Notification` → `handle_notification` — classifies the message (waiting input / permission / approval / generic) and sends a yellow/red/orange/blue card. "Waiting for input" is suppressed and reported on next Stop instead.
- `UpdateCard` → patches a previously-sent card by `message_id`. Used by external AI summarizer to enrich the card after the fact.

### Send modes

`determine_send_mode()` picks one based on config:
- **app** — uses `app_id` + `app_secret` to fetch `tenant_access_token` (cached with 5-min refresh buffer), sends via `/im/v1/messages`. Required for `update_card` (webhook mode cannot patch).
- **webhook** — sends to a custom bot URL with optional HMAC-SHA256 signature. No `message_id` returned.

App mode is preferred. Env vars (`FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_RECEIVE_ID`, etc.) override `configs/feishu_config.json`.

### Card structure (`build_feishu_card`)

Feishu card schema 2.0. Sections separated by `hr`, ending in a badge row (column_set):
**status_label → model → cost → context → timestamp**

Cost color thresholds: green < $1, orange $1–5, red > $5.
Context color thresholds: green < 30%, orange 30–70%, red > 70%.

### Cost & context estimation

`estimate_cost` and `format_context_pct` use a hardcoded pricing/context-window table keyed by model prefix. **When adding a new model, update both dicts** — they are independent and pattern-matched via `startswith`. `glm-5.1` is aliased to `claude-opus-4-7` (1M context, opus pricing) since it's a proxy.

### AI summary queue

After a successful Stop notification, raw transcript data is appended to `db/summary_queue.json` (kept to last 10 pending). An external process is expected to read this queue, generate AI summaries, and call back via `UpdateCard` to patch the card with cleaner content. `ai_summarize()` is a built-in helper (uses `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`) but the actual queue consumer is not in this repo.

## Common Commands

```bash
# Run script standalone (prints "ok")
python src/ccfeishunotify.py

# Syntax check
python -c "import py_compile; py_compile.compile('src/ccfeishunotify.py', doraise=True)"

# Manually invoke a hook event with test data
cat tests/test_data/individual_events/scenario_1/01_userpromptsubmit.json | python src/ccfeishunotify.py UserPromptSubmit

# Deploy local edits to global hook target
cp src/ccfeishunotify.py ~/.claude/ccfeishunotify/src/

# Inspect the SQLite tracking DB
sqlite3 db/ccfeishunotify.db "SELECT session_id, seq, cwd, created_at, stoped_at FROM prompt ORDER BY id DESC LIMIT 20;"
```

## Tests

`tests/test_ccfeishunotify.py` — unittest suite for config loading, DB ops, card building. **The test currently has `from ccfeishunotify import ClaudePromptTracker` with `sys.path.insert(0, parent)`, which assumes the old flat layout.** When running tests after the `src/` reorganization, the import path needs updating to point at `src/`. Don't blindly run pytest expecting it to pass.

`tests/run_tests.py` — manual scenario runner that pipes JSON fixtures into the hook. Same path-staleness caveat.

`tests/test_data/` — 6 scenarios with full event sequences and per-event JSON files for replay.

## Working Notes

- **Windows-first**: paths use backslashes in `settings.json`. PowerShell is the default shell. `find_transcript_path` lowercases the drive letter to match Claude Code's project-dir encoding (`d-MT-ccnotify`).
- **No external dependencies**: stdlib only (`urllib`, `sqlite3`, `json`, `logging`). Don't add `requests` / `httpx`.
- **Surrogate sanitization**: `sanitize_surrogates()` strips lone surrogates from input — Windows transcripts sometimes contain them and crash UTF-8 encoding without this.
- **Logging**: `TimedRotatingFileHandler` rotates `logs/ccfeishunotify.log` daily, keeps 1 backup. Don't `print()` from hook handlers — it pollutes Claude Code's UI.
