# B8: Attachments (PROPOSED)

- **Source:** no plan document. Branched 2026-10-07 at the owner's request: "branch on the attachments part, either from user's side, or claude build while responding".
- **Parent:** B7 (two-call turn protocol, E8) × B3 (turn format, slots) × B4/B5 (mirror). B0 §3 reserved the marker only: `Attachment(type, title)` (`models.py:86`) and `[type: "title" — not archived]` (`formatter.py:227`). Nothing fills it today: no tool field reaches `TurnData.attachments`.
- **Status:** ❌ proposal only. Nothing built.
- **Constraint (owner, 2026-10-05):** MCP only, deterministic. Options that move data outside MCP are listed and marked ⛔.

## What MCP can and cannot do here
- The server receives only the tool arguments the model writes, and those are text.
- **The model can resend text it has seen:** a pasted document, an uploaded `.md`/`.csv`/code file, or the source of an artifact it wrote.
- **The model cannot resend bytes:** it sees an image or a scanned PDF but cannot write out that file. Asking for base64 gives invented output.
- **Files Claude builds in code execution** (`.docx`, `.xlsx`, charts) are bytes in the sandbox. The model knows the name and can run `sha256sum`, but cannot hand the file over through a tool argument.
- **Exact bytes reach the server only if a person or the sandbox uploads them.** In MCP terms that means a link the user opens (U3/U4) or an MCP App (U5).

## Two kinds of attachment
| | User side (sent with the message) | Claude side (made in the reply) |
|---|---|---|
| Examples | image, PDF, pasted long text, `.csv`, `.md`, code file | artifact (HTML/React/SVG/Mermaid/Markdown), code-execution file (`.docx`, `.xlsx`, `.png`) |
| Text the model can resend | text files, pasted text, text-layer PDFs | artifact source, the code that made a file |
| Bytes only | images, scans, Office files | generated binaries |
| Natural call | **call 1** (start of reply: the reliable call) | **call 2** (after the reply: the call that gets skipped, P1-16), with a fallback on the next call 1 like `last_reply` (E8-7) |

## Options
Weights: fidelity 25 · reliability (no extra step the model or user may skip) 20 · user effort 15 · echo/safeguard risk 15 (P1-11) · token cost 10 · platform support verified 10 · privacy 5.

### User side
| ID | Option | How | Score | Notes |
|---|---|---|---|---|
| **U1** | **Manifest** | Call 1 gets `attachments: [{kind, name, mime, size?, pages?, note ≤200}]`. Stored as marker lines under `## User` | **78** | Always works. Records *what* was attached, not its content. Rides the reliable call |
| U2 | Text copy by the model | Text attachments only: `content` field, capped (e.g. 20 000 chars), `fidelity` = verbatim/abridged | 70 | Model may shorten without saying so (P1-12), and the server can't check. Cost grows with file size. Lower echo risk than the model resending its own output |
| **U3** | **Upload link** | Call 1 result returns a short-lived signed link per listed attachment. User opens it and drops the file. The server stores the bytes and links them to the turn | **75** | Exact bytes. Costs the user one step, and many will skip it. The page needs auth, which is paused in B5 (P0-1) |
| U4 | URL-mode elicitation | Same as U3, but the client shows the server's prompt (MCP spec 2025-11-25) | 75 | **? Unverified** whether claude.ai / Desktop support it. Cheap probe |
| U5 | MCP App drop zone | Widget in the chat; the user drops the file and the app calls `vault_attach` | 72 | Breaks on Windows Desktop (claude-ai-mcp#729, B7 path B). The user still drops the file a second time |
| U6 ⛔ | Sandbox push | With code execution on, the model runs `curl` on `/mnt/user-data/uploads/*` to the upload link | (75) | Moves data outside MCP; needs an egress allowlist and code execution. Excluded unless the owner lifts the constraint |

### Claude side
| ID | Option | How | Score | Notes |
|---|---|---|---|---|
| **C1** | **Manifest** | Call 2 gets `artifacts: [{id, kind, title, version}]`; the next call 1 repeats it as `last_artifacts` | **78** | Same as U1. Shows which artifact versions came from which turn |
| C2 | Full source each version | Call 2 `artifacts[].content` (capped, e.g. 30 000 chars) | 72 | Exact text, but this is the largest echo of Claude's own output, the P1-11 pattern. Cost doubles on every edit |
| **C3** | **Source once, then edits** | First version: full source. Later versions: the `old → new` edit pairs plus `base_sha`. The server rebuilds each version, and on a `base_sha` mismatch asks once for the full source | **77** | Matches how claude.ai updates artifacts. Small echo per edit. A missed version breaks the chain until a full resend |
| C4 | Recipe for binaries | Store the code that made the file + name + size + `sha256` (from `sha256sum` in the sandbox). Fidelity `recipe` | 70 | Can rebuild the file, not an exact copy (dependencies, time). Cheap; honest label |
| C5 ⛔ | Sandbox push of outputs | `curl` `/mnt/user-data/outputs/*` to the upload link | (75) | Same as U6 |

### Recommended: layered **M** (≈ 84)
1. **M1 = U1 + C1 manifests, always.** Schema fields on both calls; the server writes marker lines; the turn records what was attached even when nothing else is saved. Deterministic and cheap. Build first.
2. **M2 = C3** for text artifacts (Claude's own source, edits after the first version), behind `THREADVAULT_ARTIFACTS=off|manifest|source` (default `manifest` until an echo test passes, same rule as B7 E-budget).
3. **M3 = U2** for text-type user files, capped, default off. Content gets the same redaction pass as message text.
4. **M4 = U3** opt-in upload link for exact bytes (user files and generated binaries). Only after P0-1 auth is back.
5. **Probe, don't build:** U4 (URL elicitation) on claude.ai web + Desktop. If it works, it replaces U3's plain link.
6. **C4** only if the owner wants generated Office files traceable without M4.

## Storage (shared by all options)
- **Content-addressed:** `<account>/_files/<sha256[:2]>/<sha256>.<ext>`; the same bytes are stored once (retries, repeated uploads, unchanged artifact versions).
- **Turn line:** extends today's marker:
  `[image: "invoice.png" — not archived]` (manifest only)
  `[artifact: "Pricing dashboard" v3 — archived sha=1a2b3c → _files/1a/1a2b….html]`
  Front matter/turn comment keeps `attachments=N`.
- **Slots:** attachment key = `(thread, turn, side, name|artifact_id, version)`; upsert newest-wins like E2b, so call 2 and the next call 1 never duplicate.
- **Caps:** per item and per turn; over the cap → manifest only + `truncated=true`, never silent.
- **Parser:** `_ATTACHMENT_LINE_RE` (`formatter.py:87`) matches only `— not archived]`. It must accept the archived form, or the B3 round-trip test (row 85) breaks.

## Security gates (block M3/M4 until closed)
- **Mirror is public (P0-3).** `_files/` is **never** mirrored; the mirror gets marker lines only. Artifact source (M2) is text, so it would be mirrored, which makes it a privacy call for the owner.
- **Binaries can't be redacted.** Store them only privately; they don't go through `vault_find`.
- **Open `/mcp` (P0-1, auth paused).** An upload endpoint without auth is an open file host. M4 waits for auth.
- Upload links: single thread + turn, 15-min TTL, size limit, MIME allowlist, no rendering of uploaded HTML on our origin.

## Open decisions (owner)
1. Is a manifest-only baseline (M1) enough for v1?
2. Should artifact **source** be captured, given the echo risk? Default proposed: manifest until an E6b-style test passes.
3. Is one user click per file (U3) acceptable for exact bytes?
4. Should attachments ever reach the GitHub mirror? Proposed: never bytes; artifact source only if the mirror goes private.

## Change log
- 2026-10-07: branched. U1–U6, C1–C5 scored; layered M recommended; security gates listed. Nothing built.
