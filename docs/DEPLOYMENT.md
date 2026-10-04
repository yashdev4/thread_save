# ThreadVault Production Deployment Guide (§2 S7, Milestone X8)

## 1. Architecture & Latency Budget

```
Claude Cloud ──(HTTPS)──► ThreadVault Web (FastAPI) ──► PostgreSQL (Same Region)
                            [p95 < 300 ms budget]
```

To maintain high conversation fidelity, latency on the save path must satisfy **p95 < 300 ms** server-side processing time.

### Anti-Pattern: Cold Starts
Free-tier hosting platforms that sleep idle containers after 15 minutes of inactivity introduce **30–60 second cold start delays** on the user's first turn.
- **Requirement**: ThreadVault **MUST** be deployed on an **always-on paid instance** (e.g., Fly.io Dedicated/Standard Machine with `auto_stop_machines = false`, or Render Starter Plan).
- **Region**: Deploy in a US-East region (e.g. Fly.io `iad` Ashburn, AWS `us-east-1` N. Virginia) to minimize network transit time between Anthropic's cloud and ThreadVault.
- **Database Proximity**: The PostgreSQL database **MUST** reside in the same cloud region and data center as the web instance to ensure round-trip SQL latencies remain under 2–5 ms.

---

## 2. PostgreSQL Configuration, Backups & PITR

### Connection & Timeout Tuning
Configure `postgresql.conf` or cloud instance parameters:
```ini
# Statement and transaction timeouts (§3.3)
statement_timeout = 4000      # 4 seconds max per statement
idle_in_transaction_session_timeout = 5000 # 5 seconds max per idle transaction

# Concurrency & Pool
max_connections = 100
shared_buffers = 256MB
```

### Point-In-Time Recovery (PITR) & Backups
To protect user conversation archives from accidental deletions or corruption:
1. **Continuous WAL Archiving**:
   Enable Write-Ahead Log (WAL) archiving to an S3-compatible bucket (e.g. via `wal-g` or managed AWS RDS / Fly Postgres continuous archiving).
2. **Automated Daily Snapshots**:
   Nightly full physical base backups with a 30-day retention window.
3. **Restoration Testing**:
   Verify automated restore drill periodically to meet recovery point objective (RPO) < 5 minutes.

---

## 3. Deployment Runbook

### Option A: Fly.io (Recommended)

1. **Install Fly CLI**:
   ```bash
   fly auth login
   ```

2. **Launch Postgres**:
   ```bash
   fly postgres create --name threadvault-db --region iad --vm-size shared-cpu-1x --volume-size 10
   ```

3. **Deploy Web Server**:
   ```bash
   fly launch --config fly.toml
   fly secrets set THREADVAULT_JWT_SECRET=$(openssl rand -hex 32)
   fly secrets set THREADVAULT_VIEWER_SECRET=$(openssl rand -hex 32)
   fly deploy
   ```

4. **Verify Health**:
   ```bash
   curl -i https://threadvault.fly.dev/health
   # Returns 200 OK with {"status": "healthy", "database": "connected"}
   ```

### Option B: Render

1. Connect GitHub repository to Render.
2. Select **Apply Blueprint** using `render.yaml`.
3. Set region to `Ohio` (US East) for both web and database services.
4. Deploy and confirm `/health` returns 200 OK.

---

## 4. Verification & Gate (a) Checklist

- [x] Dockerfile builds cleanly with non-root user and curl healthcheck.
- [x] Multi-stage container incorporates database migrations on startup.
- [x] `/health` probes database connectivity with `SELECT 1`.
- [x] Server-side request processing p95 latency benchmarked under 100 ms (< 300 ms budget).
- [ ] **Gate (a) (User Action)**: Live cloud instance provisioned and verified on Fly.io or Render with custom domain and SSL certificate.

---

## 5. Complete Environment Variables & Secrets Reference

Every environment variable and secret required for production deployment:

| Variable | Secret / Config | Default | Required in Prod | Purpose |
|---|---|---|---|---|
| `DATABASE_URL` | Secret | `postgresql://postgres:@127.0.0.1:5432/thread_save_test` | **Yes** | Primary PostgreSQL connection string (asyncpg/Alembic). |
| `THREADVAULT_JWT_SECRET` | Secret | Ephemeral fallback | **Yes** | 32+ bytes secret used to sign and verify OAuth 2.1 JWT access tokens. Must persist across server restarts. |
| `THREADVAULT_VIEWER_SECRET` | Secret | Built-in test secret | **Yes** | 32+ bytes secret for HMAC-SHA256 signing of viewer tokens (`/v/{token}`) and download tokens (`/download/{thread_id}.md`). |
| `GOOGLE_CLIENT_ID` | Config | `""` | **Yes** | Google OAuth 2.0 Web Application Client ID for human sign-in. |
| `GOOGLE_CLIENT_SECRET` | Secret | `""` | **Yes** | Google OAuth 2.0 Web Application Client Secret for token exchange. |
| `THREADVAULT_ENFORCE_AUTH` | Config | `false` | **Yes** | When `true`, enforces OAuth 2.1 Bearer authentication on all MCP and REST endpoints. |
| `THREADVAULT_ENVELOPE_KEY` | Secret | `""` | Optional | 32-byte hex-encoded Master Key for AES-256-GCM envelope encryption at rest. |
| `THREADVAULT_SERVER_KEY` | Secret | `""` | Optional | Secret key for generating HMAC integrity tags in redaction masks (`[REDACTED:aws_access_key:xxxx]`). |
| `THREADVAULT_VIEWER_TTL_SECONDS` | Config | `900` (15 min) | No | Time-to-live in seconds for viewer URLs and download links. |
| `THREADVAULT_MODE` | Config | `turn_start` | No | Reliability protocol mode: `turn_start` (default) or `both`. |
| `THREADVAULT_NUDGE` | Config | `off` | No | Prompt nudge mode: `off` (default) or `on`. |
| `PORT` | Config | `8000` | No | HTTP port for Uvicorn web server. |

---

## 6. Google OAuth 2.0 Upstream IdP Configuration

ThreadVault uses Google as its primary upstream Identity Provider (IdP) so human sign-in produces a stable `account_id` keyed strictly on Google's `sub` claim (never email).

### Step-by-Step Google Cloud Console Setup

1. **Create Google Cloud Project**:
   - Go to [Google Cloud Console](https://console.cloud.google.com/).
   - Create a new project named `ThreadVault`.

2. **Configure OAuth Consent Screen**:
   - Navigate to **APIs & Services** > **OAuth consent screen**.
   - User Type: Select **External** (or **Internal** for Google Workspace orgs).
   - App Name: `ThreadVault`.
   - User support email: Select your admin email.
   - Authorized domains: Add your deployed domain (e.g. `fly.dev` or `yourdomain.com`).
   - Scopes: Request only minimal identity scopes:
     - `openid` (view your general account info)
     - `https://www.googleapis.com/auth/userinfo.email` (`email`)
     - Do NOT request `profile` or extended Google Drive/Gmail scopes.

3. **Add Test Users (while in Testing status)**:
   - Under **Test users**, click **Add Users**.
   - Add your personal Google account email(s).

4. **Create OAuth 2.0 Credentials**:
   - Navigate to **APIs & Services** > **Credentials**.
   - Click **Create Credentials** > **OAuth client ID**.
   - Application type: **Web application**.
   - Name: `ThreadVault Web Client`.
   - **Authorized redirect URIs**:
     - Exact production URI: `https://<your-domain>/oauth/callback/google`
     - Local dev URI: `http://localhost:8000/oauth/callback/google`
   - Click **Create**. Copy the **Client ID** and **Client Secret**.

5. **Set Production Secrets**:
   ```bash
   fly secrets set GOOGLE_CLIENT_ID="<client-id>.apps.googleusercontent.com"
   fly secrets set GOOGLE_CLIENT_SECRET="<client-secret>"
   ```
