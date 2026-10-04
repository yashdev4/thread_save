# GitHub Archive Export: Live Verification Guide (Milestone GH8)

This guide documents the procedures for live end-to-end verification of the GitHub Archive Export system against a real private GitHub repository.

---

## 1. Prerequisites (User Setup)

To verify GitHub Archive Export against live GitHub APIs without compromising production repositories or data:

### 1.1 Create a Dedicated Private Throwaway Repository
1. Navigate to GitHub -> **New repository**.
2. Name: e.g. `threadvault-archive-test` (or any preferred name).
3. **Visibility**: Select **Private** (ThreadVault strictly enforces private repositories and will reject public repos to prevent secret leakage).
4. **Initialize with README**: Checked (ensures the default `main` branch exists).

### 1.2 Generate a Fine-Grained Personal Access Token (PAT)
1. Go to **GitHub Settings** -> **Developer Settings** -> **Personal access tokens** -> **Fine-grained tokens**.
2. Click **Generate new token**.
3. **Token name**: `ThreadVault Archive Live Verification`.
4. **Expiration**: 7 to 30 days (or desired duration).
5. **Repository access**: Select **Only select repositories** -> pick `threadvault-archive-test`.
6. **Permissions**:
   - **Repository permissions**:
     - `Contents`: **Read and write** (allows reading tree and pushing commits via Git Data API).
     - `Metadata`: **Read-only** (mandatory, granted automatically).
7. Generate token and copy the string (`github_pat_...`).

---

## 2. Running Live Verification

### Option A: Via the Standalone Verification Script

Run the automated test runner:
```bash
python scripts/verify_github_live.py --repo "<owner>/<repo>" --token "<github_pat_...>"
```

Or set the environment variables:
```bash
# Windows PowerShell
$env:THREADVAULT_GH_LIVE_REPO = "myorg/threadvault-archive-test"
$env:THREADVAULT_GH_LIVE_TOKEN = "github_pat_..."
python scripts/verify_github_live.py

# Linux/macOS
export THREADVAULT_GH_LIVE_REPO="myorg/threadvault-archive-test"
export THREADVAULT_GH_LIVE_TOKEN="github_pat_..."
python scripts/verify_github_live.py
```

### Option B: Via Pytest Live Integration Suite

When `THREADVAULT_GH_LIVE_REPO` and `THREADVAULT_GH_LIVE_TOKEN` are exported in the environment, run:
```bash
pytest tests/test_github_live.py -v
```

---

## 3. What the Live Verification Exercises

1. **Phase 1: Privacy Guard Verification (G6)**
   - Queries `GET /repos/{owner}/{repo}`.
   - Verifies that `private: true`.
   - Confirms that any accidental change to a public repository immediately stops export pushes.

2. **Phase 2: First Sync Batch (G1, G3)**
   - Generates archive tree layout (`README.md`, `index/threads.json`, monthly Markdown index, thread pages).
   - Executes the 3-request Git Data API sequence (`GET ref` -> `POST /git/trees` -> `POST /git/commits` -> `PATCH /git/refs/heads/main`).
   - Verifies inline tree blobs without needing local git clones.

3. **Phase 3: Incremental Sync with Sticky Page Skip (G4)**
   - Appends a new turn/page to an existing thread.
   - Computes page hashes against the export manifest.
   - Verifies that closed/unchanged pages (Page 1) are skipped and only new/modified pages (Page 2 + updated indexes) are committed.

4. **Phase 4: Remote Human-Edit Conflict Safety (G5)**
   - Tests detection of remote modifications by comparing base tree blob SHAs with `last_exported_blob_sha`.
   - Verifies that human edits made on GitHub are never overwritten, flagged as conflicts, and omitted from the push batch.

5. **Phase 5: Git History Squash (G6)**
   - Executes `squash_history()`.
   - Creates a parentless orphan root commit with the exact current tree.
   - Force-updates `refs/heads/main`, purging all prior intermediate commit history from the branch.
