# ThreadVault Onboarding & Migration Guide

> **Architecture:** Remote Streamable HTTP MCP Server backed by PostgreSQL (`PgStore`).
> **Compatibility:** Claude Web, Claude Desktop, Claude Android, and Claude iOS from a single setup.

---

## 1. Upgrade from Local Stdio (Critical for Existing Users)

If you previously used the local stdio version of ThreadVault:

> **IMPORTANT:** You **must remove** the stdio entry from your `claude_desktop_config.json`.
>
> If you keep both the stdio entry and the new remote connector, Claude Desktop will register duplicate tools with identical names (`vault_save_turn`, `vault_find`), leading to split archives and erratic model behavior.

### Removing the Stdio Entry
1. Open your Claude Desktop configuration file:
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Linux:** `~/.config/Claude/claude_desktop_config.json`
2. Remove the `"threadvault"` block from `"mcpServers"`. If no other servers are present, leave:
   ```json
   {
     "mcpServers": {}
   }
   ```
3. Restart Claude Desktop.

---

## 2. Migrate Your Existing Local Vault

Use the `thread-save-migrate` tool to import your historical Markdown conversation archives into your hosted PostgreSQL instance.

### Preview Migration (Dry Run)
Inspect your Markdown vault files without modifying the database:
```bash
python -m thread_save.cli.migrate --vault-dir "path/to/your/threads_vault" --dry-run
```

### Run Migration
Import all threads, turns, stubs, and metadata into PostgreSQL:
```bash
python -m thread_save.cli.migrate \
  --vault-dir "path/to/your/threads_vault" \
  --account-slug "your-account-slug" \
  --dsn "postgresql://user:password@db-host:5432/thread_save"
```

The migration tool:
- Groups pages by `thread_id` and sorts turns chronologically.
- Resolves highest fidelity when slots appear across pages.
- Preserves stubs, gap tracking, and tombstone states.
- Is fully **idempotent**: re-running the migration will safely update without duplicating rows.

---

## 3. Connect ThreadVault (Single Setup for All Devices)

Anthropic custom connectors are remote MCP servers configured through the Claude web interface or Desktop client. Mobile apps (iOS & Android) inherit connectors automatically.

### Step A: Add Connector on Web or Desktop
1. Open [claude.ai](https://claude.ai) or **Claude Desktop**.
2. Navigate to **Customize / Settings → Connectors → Add custom connector**.
3. Enter your remote server URL:
   ```text
   https://<your-threadvault-domain>/mcp
   ```
4. Complete the OAuth 2.1 login flow.

### Step B: Configure Permissions
On your first conversation turn, Claude will request permission to use the tools:
- Select **"Always allow"** for `vault_save_turn` and `vault_backfill`.
- *Note:* Requiring manual approval on every turn will cause missed saves and prompt fatigue.

### Step C: Add Personal Preference
In Claude's **Settings → Preferences** (Personal Preferences), paste the following directive:
```text
I use the ThreadVault connector to archive my chats. When it's available, call
vault_save_turn at the start of each reply as its description explains. If I say "don't save
this chat", skip it for that conversation.
```
This preference syncs across all devices and instructs Claude to log turns routinely.

### Step D: Mobile Verification (iOS & Android)
Open the Claude app on your phone or tablet:
- The custom connector is already enabled for your account.
- Conversation saves and cross-device continuations function automatically.

---

## 4. Features & Usage

### Web Viewer Links
When searching your archive via `vault_find`, hits include secure, short-lived HMAC-signed viewer links:
```text
https://<your-threadvault-domain>/v/<signed-token>
```
Open this link in any browser to view a clean transcript with turn timestamps, fidelity badges, and full styling.

### Raw Markdown Downloads
Each thread can be downloaded directly as a canonical Plan v2 Markdown file:
```text
https://<your-threadvault-domain>/download/<thread-id>.md
```

### Opt-Out & Pause Controls
- **Per Conversation:** Say *"don't save this chat"*, *"stop archiving"*, or *"don't log this"*. ThreadVault pauses the thread and writes nothing further.
- **Global Kill Switch:** The server operator can set `paused: true` or touch `vault/.paused` to pause all active logging.

---

## 5. External Exporters (Optional)

Configure Google Drive or GitHub synchronization in your ThreadVault account settings:
- The server acts as a single writer, uploading rendered Markdown files 2 minutes after a conversation becomes idle.
- Hash-based idempotency prevents redundant commits or uploads.
