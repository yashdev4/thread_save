# ThreadVault Onboarding Checklist

## 1. Install the MCP Server

Add the following to your `claude_desktop_config.json`
(located at `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "threadvault": {
      "command": "C:\\Python314\\python.exe",
      "args": ["-m", "thread_save.server"],
      "env": {
        "PYTHONPATH": "D:\\Dev Projects\\2026\\eoxs\\thread save\\src",
        "THREAD_SAVE_VAULT_ROOT": "D:\\Dev Projects\\2026\\eoxs\\thread save\\threads_vault",
        "THREAD_SAVE_ACCOUNT": "dhanshree"
      }
    }
  }
}
```

## 2. Set Permissions

On first tool prompt in Claude Desktop, choose **"Allow always"** for `vault_save_turn`
and `vault_backfill`. A per-call approval prompt will drive coverage to zero within a day.

## 3. Add User Preference

Paste this into Claude's personal preferences (Settings → User Preferences):

```
I use the ThreadVault connector to archive my chats locally. When it's available, call
vault_save_turn at the start of each reply as its description explains. If I say "don't save
this chat", skip it for that conversation.
```

## 4. Verify

Run a 10-turn test conversation, then check coverage:

```bash
python -m thread_save.report
```

You should see:
- `coverage_pct` ≥ 90% (non-stub user turns / total user turns)
- `split_rate_pct` < 5% (threads created by anchor miss / all threads)
- Zero `failed_calls`
- Zero cross-thread contamination

## Configuration

| Variable | Default | Description |
|---|---|---|
| `THREAD_SAVE_VAULT_ROOT` | `~/thread_vault` | Where thread files are stored |
| `THREAD_SAVE_ACCOUNT` | `default` | Account partition name |
| `THREADVAULT_MODE` | `turn_start` | `turn_start` or `both` |
| `THREADVAULT_NUDGE` | `off` | `off` or `on` (adds `log_next_turn` to results) |

## Opt-Out

- **Per conversation**: Say "don't save this chat" and the thread is paused.
- **Global**: Create an empty file at `vault/.paused` to pause all writing.
