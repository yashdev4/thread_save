# Cross-Surface Testing & Synchronization Verification Runbook

> **Companion to:** `cross_device_sync_plan.md` (§4.4 Milestone X9) and `invocation_reliability_impl_plan.md` (§8.2).
> **Objective:** Verify that ThreadVault functions identically and continuously across **Claude Web**, **Android**, **iOS**, and **Claude Desktop** from a single remote connector setup.

---

## 1. Synchronization Architecture Overview

ThreadVault relies on key platform mechanics verified against Anthropic's remote MCP architecture:

1. **Single Hosted Writer (`PgStore`):** All conversational writes from every surface flow to a single remote Streamable HTTP endpoint backed by PostgreSQL. There are no local files to reconcile and no Drive sync conflict files.
2. **Anthropic Cloud Origin:** All tool invocation calls originate from Anthropic's cloud infrastructure, regardless of whether the user is on Claude Web, Desktop, iOS, or Android.
3. **Contextual Thread Continuity:** Because Claude natively syncs active conversation contexts across devices, switching from Desktop to mobile maintains the conversational history, including `thread_id` and previous turn anchors. Thread continuity is preserved by design without requiring client device fingerprinting (S5).
4. **"Split, Never Merge" (I-3):** Anchor matching binds turns unambiguously. If a turn cannot be safely bound to an existing thread, it creates an isolated thread rather than risking cross-chat pollution.

---

## 2. Onboarding & Setup Across Surfaces

### Step 1: Add Custom Connector (Once on Web or Desktop)
1. Open [claude.ai](https://claude.ai) or **Claude Desktop**.
2. Navigate to **Customize / Settings → Connectors → Add Custom Connector**.
3. Enter your ThreadVault URL:
   ```text
   https://<your-threadvault-domain>/mcp
   ```
4. Authenticate via OAuth 2.1 (RFC 7636 PKCE code exchange).

### Step 2: Configure Permissions
1. On your first conversation, Claude will prompt for tool execution approval.
2. Set `vault_save_turn` and `vault_backfill` to **"Always allow"**.
   > *Note:* Per-call prompts will quickly derail automatic conversational logging.

### Step 3: Add Global User Preference
In Claude's **Personal Preferences** (Settings → Preferences), add the following directive:
```text
I use the ThreadVault connector to archive my chats. When it's available, call
vault_save_turn at the start of each reply as its description explains. If I say "don't save
this chat", skip it for that conversation.
```

### Step 4: Mobile Inheritance (iOS & Android)
- Open the Claude app on iOS or Android logged into the same Anthropic account.
- The custom connector is **automatically inherited** from your account configuration.
- No local server setup or mobile installation is needed.

---

## 3. Automated Cross-Surface Test Runner (Regression Test)

A deterministic regression test script is provided in `scripts/cross_surface_test.py` to validate multi-device continuity, secret redaction, and chunk assembly across simulated Desktop, Android, iOS, and Web clients.

### Running Against Local Test Database (Regression Test Mode)
```bash
python scripts/cross_surface_test.py
```

### Running Against Live Remote Server (HTTP /mcp Mode)
```bash
python scripts/cross_surface_test.py --url https://<your-threadvault-domain> --account your-account-slug
```

### What the Regression Script Verifies:
- **Turns 1–5 (Claude Desktop Simulation):** Initiates session, tests turn-start lagged protocol, injects a test API key to verify pre-insert redaction, and streams a >6000 character code block in multiple chunks.
- **Turns 6–8 (Claude Android Simulation):** Simulates user continuing the conversation on an Android phone, using the same `thread_id` and anchors, plus tests consecutive "continue" messages (W-5 idempotency key).
- **Turn 9 (Claude iOS Simulation):** Continues conversation on an iPhone.
- **Turn 10 (Claude Web Simulation):** Finalizes conversation on Claude Web.
- **Invariants Checked:**
  - Thread count = 1 (0 split threads, split rate = 0.0%).
  - Total turns ≥ 10 with dense slot numbering (1..10) and 0 stubs (100% coverage).
  - API keys masked with `[REDACTED:...]` and absent in plaintext.
  - Chunked assistant reply assembled completely and stored verbatim.

---

## 4. Manual Live Scenario Protocol (§8.2)

To manually certify a live production deployment across physical devices using live remotely exposed endpoints:

### Phase A: Desktop Initiation (Turns 1–5)
1. In Claude Desktop, start a new chat:
   > *"Let's design a distributed event-driven system architecture."*
2. Verify via the live remotely exposed server:
   - Live health check: `GET https://<your-domain>/health` returns HTTP 200 with `status: "healthy"` and `database: "connected"`.
   - Tool execution: In Claude Desktop, `vault_save_turn` returns `{"ok": true, "thread_id": "...", "n": 1}`.
3. Continue for 3 turns on technical architecture details.
4. **Redaction test:** In Turn 4, mention a test key:
   > *"For our sink connector, use api_key = 'test_key_live_abcdef1234567890abcdef1234567890'."*
5. **Chunking test:** Ask for a large output:
   > *"Generate a 500-line complete Python Kafka consumer with exhaustive docstrings."*
6. Conclude the desktop phase:
   > *"I am switching to my mobile phone now."*

### Phase B: Mobile Continuation (Turns 6–10)
1. Immediately open the **Claude Android** or **Claude iOS** app.
2. Select the existing conversation from your chat history.
3. Send a follow-up query:
   > *"Now on my phone. What are the recommended DLQ retry policies for this consumer?"*
4. Send two consecutive turns of:
   > *"continue"*
5. Send final wrap-up prompt.

### Phase C: Live Remote Verification & Inspection
All inspection is performed strictly against live remotely exposed HTTP endpoints:
1. **Live Remote Stats Tool (`vault_stats` over `/mcp`):**
   In Claude or via MCP client, invoke `vault_stats(thread_id="<thread_id>")`:
   - `ok: true`
   - `total_turns >= 10`
   - `coverage_pct >= 90.0%`
   - `split_rate < 5.0%`
   - `gaps_open: []` (0 open gaps)
   - `open_turn: null` (turn closed cleanly upon wrap-up)
2. **Live Remote Find Tool (`vault_find` over `/mcp`):**
   Invoke `vault_find(query="Distributed Architecture")`:
   - `ok: true`
   - Returns search hit with `viewer_url` (`/v/{token}`) and `download_url` (`/download/{thread_id}.md?token={token}`)
   - Declares `search_mode: "full_text"` (or `search_mode: "titles_only"` if envelope encryption is active, with explanatory non-empty notice)
3. **Live Web Viewer (`GET https://<your-domain>/v/{token}`):**
   - Open the viewer URL in any mobile or desktop web browser.
   - Verify that all 10 turns from both Desktop and Mobile appear in a single, contiguous, chronologically ordered HTML transcript.
   - Verify that the test API key is displayed as `[REDACTED:...]` (raw key never rendered).
   - Verify that the >6,000 character Kafka consumer code block is rendered complete and intact.
4. **Live Markdown Export (`GET https://<your-domain>/download/{thread_id}.md?token={token}`):**
   - Download the raw markdown archive.
   - Verify Plan v2 YAML front matter (`schema_version: 2`, `thread_id`, `account`, `title`).
   - Verify Plan v2 turn delimiters (`<!-- turn i=... -->` and `<!-- /turn i=... -->`).
   - Replay check: attempting to download again with the same token returns HTTP 403 Forbidden (single-use token protection).

---

## 5. External Gate (e) Verification Checklist

Before full production launch, confirm each live remotely exposed feature:

- [ ] **Live Health Probe:** `GET https://<domain>/health` returns `status: "healthy"` and `database: "connected"`.
- [ ] **Connector Visibility:** Remote connector URL `https://<domain>/mcp` active on Claude Web and Claude Desktop.
- [ ] **Mobile Functionality:** Tool calls (`vault_save_turn`) observed firing on live iOS and Android apps.
- [ ] **Cross-Device Continuity:** Switching from Desktop to Mobile mid-conversation updates the exact same `thread_id`.
- [ ] **Zero Splits:** Single canonical thread maintained across device boundaries (`split_rate: 0%` via `vault_stats`).
- [ ] **Pre-Insert Redaction:** Sensitive patterns scrubbed in live database and displayed as `[REDACTED:...]` in `/v/{token}`.
- [ ] **Live Viewer Accessibility:** Signed HMAC web links (`/v/{token}`) render properly on both mobile and desktop browsers.
- [ ] **Secure Download Stream:** `/download/{thread_id}.md?token={token}` delivers valid Plan v2 markdown with single-use replay protection.
