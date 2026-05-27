# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

CCFeishuNotify is a Claude Code hook script that sends Feishu (Lark) interactive card notifications when Claude Code submits prompts, completes tasks, or requires user input/permission. It is invoked by Claude Code's hook system (UserPromptSubmit / Stop / Notification events) via stdin JSON.

The script is **single-file by design** — all logic lives in `src/ccfeishunotify.js`. There is no build step. **Node.js only** — no Python version exists in this repo.

## Directory Layout (load-bearing)

```
src/ccfeishunotify.js           # main script (Node.js, zero external deps)
configs/feishu_config.json      # credentials (gitignored)
configs/feishu_config.example.json
db/ccfeishunotify_state.json    # JSON-based session tracking
db/summary_queue.json           # AI summary queue
logs/ccfeishunotify.log         # daily-rotated log
```

The script auto-detects whether it lives under a `src/` subdirectory and resolves `configs/`, `db/`, `logs/` relative to the project root (`ClaudePromptTracker.constructor`). When invoked as a hook, the project root is `~/.claude/ccfeishunotify/`. **Do not hardcode paths or assume cwd** — use `this.project_dir`.

## Deployment Model

- The repo at `D:\MT\ccnotify` is the source of truth.
- The script is deployed by **copying** `src/`, `configs/`, `db/`, `logs/` into `~/.claude/ccfeishunotify/` (Windows: `C:\Users\Administrator\.claude\ccfeishunotify\`).
- Hooks in `~/.claude/settings.json` invoke `node "<dest>\src\ccfeishunotify.js" <EventName>`.
- The script also auto-registers hooks via `ensure_hooks_registered()` and `ensure_hooks_in_ccswitch()` (cc-switch compatibility).
- After editing `src/ccfeishunotify.js`, copy it to the deploy target to take effect:
  ```bash
  cp src/ccfeishunotify.js ~/.claude/ccfeishunotify/src/
  ```
- Verify with `node ~/.claude/ccfeishunotify/src/ccfeishunotify.js` — should print `ok`.

## Architecture

The script entry point is `main()`, which dispatches by `process.argv[2]`:
- `UserPromptSubmit` → `handle_user_prompt_submit` — inserts a record into the JSON state file; auto-computes `seq` per session.
- `Stop` → `handle_stop` — finds the latest open prompt for the session, computes duration, parses the Claude transcript JSONL (`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`) for tokens / steps / files / commands, and sends a green "completed" card. Uses `ai_summarize(prompt, "task")` for Chinese task understanding, and `ai_summarize_steps()` for step-level Chinese descriptions.
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

Each execution step is a complete section (not split by file/command/decision type):
- **第1步：中文描述** — AI-generated concise Chinese explanation for non-technical readers
- Files within step: edit=yellow, new=green, read=grey
- Commands within step: translated to Chinese

Cost color thresholds: green < $1, orange $1–5, red > $5.
Context color thresholds: green < 30%, orange 30–70%, red > 70%.
Timestamp uses UTC+8.

### Cost & context estimation

`estimate_cost` and `format_context_pct` use a hardcoded pricing/context-window table keyed by model prefix. **When adding a new model, update both dicts** — they are independent and pattern-matched via `startsWith`. `glm-5.1` is aliased to `claude-opus-4-7` (1M context, opus pricing) since it's a proxy.

### Task understanding

`ai_summarize(prompt, "task")` generates a concise Chinese summary of the user's input prompt. Falls back to keyword translation (`translate_to_chinese`) if AI is unavailable.

### Execution steps

`parse_transcript_summary` produces a `steps` array, one per assistant message. `_consolidate_steps` merges consecutive read-only steps into significant steps (edits/writes/commands). `ai_summarize_steps` batch-summarizes all steps into Chinese in one AI call. Falls back to `translate_to_chinese` per step if AI fails.

### AI summary queue

After a successful Stop notification, raw transcript data is appended to `db/summary_queue.json` (kept to last 10 pending). An external process is expected to read this queue, generate AI summaries, and call back via `UpdateCard` to patch the card with cleaner content. `ai_summarize()` is a built-in helper (uses `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`) but the actual queue consumer is not in this repo.

### cc-switch compatibility

`ensure_hooks_registered()` and `ensure_hooks_in_ccswitch()` auto-register hook entries in `~/.claude/settings.json`, `~/.claude/settings.local.json`, and cc-switch's SQLite database. This prevents hook loss when cc-switch overwrites settings.

## Common Commands

```bash
# Run script standalone (prints "ok", also auto-registers hooks)
node src/ccfeishunotify.js

# Syntax check
node --check src/ccfeishunotify.js

# Manually invoke a hook event with test data
cat tests/test_data/individual_events/scenario_1/01_userpromptsubmit.json | node src/ccfeishunotify.js UserPromptSubmit

# Deploy local edits to global hook target
cp src/ccfeishunotify.js ~/.claude/ccfeishunotify/src/

# Inspect the JSON state tracking
cat db/ccfeishunotify_state.json | node -e "const d=JSON.parse(require('fs').readFileSync('/dev/stdin','utf8')); console.table(d.prompts.slice(-20))"
```

## Working Notes

- **Windows-first**: paths use backslashes in `settings.json`. PowerShell is the default shell. `find_transcript_path` lowercases the drive letter to match Claude Code's project-dir encoding (`d-MT-ccnotify`).
- **No external dependencies**: Node.js built-in modules only (`fs`, `path`, `https`, `http`, `crypto`, `os`). Don't add `axios` / `node-fetch` / `better-sqlite3`.
- **State tracking**: JSON file-based (`ccfeishunotify_state.json`), not SQLite. Atomic writes via tmp+rename pattern.
- **All timestamps**: UTC+8 via `now_iso()`.
- **Surrogate sanitization**: `sanitize_surrogates()` strips lone surrogates from input.
- **Logging**: Daily rotation via date check in `_log()`, keeps 1 backup. Don't `console.log()` from hook handlers — it pollutes Claude Code's UI.
- **Async**: `handle_stop` and AI-related methods are async. `send_notification` returns a Promise.