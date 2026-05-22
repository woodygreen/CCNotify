# CCFeishuNotify

CCFeishuNotify provides Feishu (Lark) notifications for Claude Code, alerting you when Claude needs your input or completes tasks. Based on CCNotify.

**Cross-platform**: Works on Windows, macOS, and Linux (no macOS desktop notification dependency).

## Features

- Task completion notification with duration and prompt summary
- Permission/action required notification with color-coded cards
- Feishu interactive card messages with rich formatting (project, path, timestamp)
- Two sending modes: App API (tenant_access_token) and Webhook (custom bot)
- Local SQLite session tracking (no data uploaded externally)

## Important Notes

Starting from claude-code v1.0.95 (2025-08-31), any invalid settings in `~/.claude/settings.json` will disable hooks. See [Why not working](#why-not-working) for solutions.

## Configuration

CCFeishuNotify supports two notification modes. Choose one based on your setup:

### Mode 1: App API (Recommended)

Uses Feishu App credentials to send messages directly to a user or group chat via the IM API. This mode supports sending to specific users by open_id, user_id, email, or chat_id.

Create `configs/feishu_config.json` (under the project root):

```json
{
  "app_id": "cli_your_app_id",
  "app_secret": "your_app_secret",
  "receive_id": "ou_your_open_id",
  "receive_id_type": "open_id",
  "webhook_url": "",
  "webhook_secret": ""
}
```

**receive_id_type options**:
- `open_id` - Feishu open ID (recommended)
- `user_id` - Feishu user ID
- `union_id` - Feishu union ID
- `email` - Feishu account email
- `chat_id` - Group chat ID

**Required App permissions**: `im:message:send_as_bot` scope must be enabled for your Feishu App.

### Mode 2: Webhook (Simple)

Uses a Feishu custom bot webhook URL. Simpler to set up but only sends to the group where the webhook is configured.

```json
{
  "app_id": "",
  "app_secret": "",
  "receive_id": "",
  "receive_id_type": "open_id",
  "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/your_hook_id",
  "webhook_secret": "your_webhook_secret"
}
```

### Environment Variable Override

All config fields can be overridden via environment variables (takes priority over config file):

- `FEISHU_WEBHOOK_URL`
- `FEISHU_WEBHOOK_SECRET`
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_RECEIVE_ID`
- `FEISHU_RECEIVE_ID_TYPE`

## Installation Guide

### 1. Install CCFeishuNotify

**macOS/Linux**:
```bash
mkdir -p ~/.claude/ccfeishunotify/{src,configs,db,logs}
ln -f src/ccfeishunotify.py ~/.claude/ccfeishunotify/src/
ln -f configs/feishu_config.json ~/.claude/ccfeishunotify/configs/
chmod a+x ~/.claude/ccfeishunotify/src/ccfeishunotify.py

# verify installation
python ~/.claude/ccfeishunotify/src/ccfeishunotify.py
# should print: ok
```

**Windows (PowerShell)**:
```powershell
$dest = "$env:USERPROFILE\.claude\ccfeishunotify"
New-Item -ItemType Directory -Force -Path "$dest\src","$dest\configs","$dest\db","$dest\logs"
Copy-Item src\ccfeishunotify.py "$dest\src\"
Copy-Item configs\feishu_config.json "$dest\configs\"

# verify installation
python "$dest\src\ccfeishunotify.py"
# should print: ok
```

### 2. Configure Feishu Credentials

Copy `configs/feishu_config.example.json` to `configs/feishu_config.json` and fill in your credentials:

```json
{
  "app_id": "cli_your_app_id",
  "app_secret": "your_app_secret",
  "receive_id": "ou_your_open_id",
  "receive_id_type": "open_id",
  "webhook_url": "",
  "webhook_secret": ""
}
```

### 3. Configure Claude Hooks

Add the following hooks to your Claude Code settings:

**macOS/Linux** - edit `~/.claude/settings.json`:

```json
"hooks": {
  "UserPromptSubmit": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python ~/.claude/ccfeishunotify/src/ccfeishunotify.py UserPromptSubmit"
        }
      ]
    }
  ],
  "Stop": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python ~/.claude/ccfeishunotify/src/ccfeishunotify.py Stop"
        }
      ]
    }
  ],
  "Notification": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python ~/.claude/ccfeishunotify/src/ccfeishunotify.py Notification"
        }
      ]
    }
  ]
}
```

**Windows** - edit `%USERPROFILE%\.claude\settings.json`:

```json
"hooks": {
  "UserPromptSubmit": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python %USERPROFILE%\\.claude\\ccfeishunotify\\src\\ccfeishunotify.py UserPromptSubmit"
        }
      ]
    }
  ],
  "Stop": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python %USERPROFILE%\\.claude\\ccfeishunotify\\src\\ccfeishunotify.py Stop"
        }
      ]
    }
  ],
  "Notification": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python %USERPROFILE%\\.claude\\ccfeishunotify\\src\\ccfeishunotify.py Notification"
        }
      ]
    }
  ]
}
```

## Try It Out

Start a new Claude Code session and run:
```
after 1 second, echo 'hello'
```
You should receive a Feishu card notification when the task completes.

## Notification Card Types

| Event | Card Color | Content |
|-------|-----------|---------|
| Task completed (Stop) | Green | Project name, job number, duration, prompt summary |
| Permission required | Red | Project name, permission request info |
| Action required | Orange | Project name, action choice info |
| Generic notification | Blue | Project name, notification message |

## How It Works

ccfeishunotify tracks Claude sessions and provides Feishu notifications at key moments:

- **When you submit a prompt**: Records the start time and project context in SQLite
- **When Claude completes**: Calculates duration, sends green card with task summary
- **When Claude needs input/permission**: Sends appropriately colored card immediately

All activity is logged locally in `logs/` and session data is stored in `db/ccfeishunotify.db`. No data is uploaded or shared externally.

## Project Structure

```
ccnotify/
├── src/                  # Main scripts
│   └── ccfeishunotify.py # Core notification script
├── configs/              # Configuration files
│   ├── feishu_config.json
│   └── feishu_config.example.json
├── db/                   # Database & queue data
│   ├── ccfeishunotify.db
│   └── summary_queue.json
├── logs/                 # Log files (auto-rotated daily)
│   └── ccfeishunotify.log
├── tests/                # Test suite
├── README.md
└── LICENSE
```

## Why not working

1. Ensure hooks configuration is active. Run `claude -p --model haiku -d hooks --verbose "hi"` to verify:

Expected output:
```
[DEBUG] Found 1 hook commands to execute
[DEBUG] Executing hook command: ccfeishunotify.py UserPromptSubmit with timeout 60000ms
[DEBUG] Hook command completed with status 0: ccfeishunotify.py UserPromptSubmit
```

If you see `Found 0 hook commands`, check for `Invalid settings` errors in your `settings.json`.

2. Ensure Feishu config is correct. Check `ccfeishunotify.log` for error messages:
   - `Tenant access token refreshed successfully` - App mode working
   - `Feishu notification sent via App mode` - Message delivered
   - `Failed to get tenant_access_token` - App credentials incorrect or permissions missing

## Uninstall

1. Edit `~/.claude/settings.json` (or `%USERPROFILE%\.claude\settings.json` on Windows) and remove all hook commands related to `ccfeishunotify`.
2. Remove all files:

**macOS/Linux**: `rm -rf ~/.claude/ccfeishunotify`

**Windows**: `Remove-Item -Recurse -Force "$env:USERPROFILE\.claude\ccfeishunotify"`

## Migration from CCNotify (macOS)

If you were using the original CCNotify with `terminal-notifier`:

1. Remove `terminal-notifier` hook commands from `settings.json`
2. Remove `~/.claude/ccnotify` directory
3. Follow the CCFeishuNotify installation guide above
4. The SQLite database format is compatible - old tracking data is not affected