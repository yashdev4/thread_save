# Branches: plan → implementation map + timeline

Each original plan document was split off from the one before it because it covered a heavily weighted area. This folder has **one file per branch**. Each file lists what that plan decided, what the code does today (with `file:line` evidence), and what later work replaced, revised or split off from each decision. [TIMELINE.md](TIMELINE.md) is the single chronological log across all branches.

## Branch tree

```
B0  thread_saving_mcp_plan_v2.md ............ foundation (stdio, files, format, tools)
│
├── B1  invocation_reliability_impl_plan.md ... §8 + §11 "25% invocation reliability"
│        (replaced B0 §2 thread_open/active.json, §5 tool names, §7.4 ring buffer, §8 log_next_turn)
│
├── B2  cross_device_sync_plan(_2).md ......... §9 multi-machine → "any device" = remote server
│   │    (replaced B0 §9 machine partition; moved canonical store files → Postgres)
│   │
│   ├── B3  write_path_correctness_plan.md .... §11 "25% write-path" re-scoped for Postgres
│   │        (executes inside B2 X2 + X5; defines W-1…W-11)
│   │
│   └── B4  github_archive_export_plan.md ..... B2 S2/X7 exporter, GitHub half
│            (also adds G7 local offload for B0 stdio users)
│
├── B5  standalone-deploy (UNPLANNED) .......... appeared in commits on 2026-10-05
│        FileStore on Render + auth paused + 60 s GitHub mirror loop.
│        Overrides B2 S1/S4/S7 and bypasses B4 G4/G5 in production.
│
├── B6  local-capture-fidelity (PROPOSED) ...... B1 × B3 field report, 2026-10-05
│        FileStore restart amnesia, last reply never saved, header-only files.
│        Fix also applies to B5 (same FileStore).
│
└── B7  echo-free-capture (PROPOSED, v2) ....... replaces B1 §2/§4/§6 protocol; supersedes B6 L3
         MCP-only, deterministic. Paths A–E scored; E (layered) recommended.
         (v1 "deterministic-capture" = source capture outside MCP: 🔀 replaced, TIMELINE 66–67)
```

| ID | File | Source plan | Weight in parent | Status summary |
|---|---|---|---|---|
| B0 | [B0-foundation-plan-v2.md](B0-foundation-plan-v2.md) | `thread_saving_mcp_plan_v2 by claude.md` | root | Mostly built; much of it replaced by B1/B2/B3 |
| B1 | [B1-invocation-reliability.md](B1-invocation-reliability.md) | `invocation_reliability_impl_plan.md` | 25% | R1–R6 built; **R7 live baseline not done** |
| B2 | [B2-cross-device-sync.md](B2-cross-device-sync.md) | `cross_device_sync_plan.md`, revised by `cross_device_sync_plan_2.md` | §9 | X1–X10 built and tested locally; **gates a/b/e/f open**; not what is deployed |
| B3 | [B3-write-path-correctness.md](B3-write-path-correctness.md) | `write_path_correctness_plan.md` | 25% | W1–W6 largely built; W7 restore drill not done; gaps found |
| B4 | [B4-github-archive-export.md](B4-github-archive-export.md) | `github_archive_export_plan.md` | B2 S2 | GH1–GH7 built as a library; **not wired to the deployed server**; GH8 gate open |
| B5 | [B5-standalone-deploy.md](B5-standalone-deploy.md) | none (emerged in commits) | — | **Live in production**; untested; P0 issues |
| B6 | [B6-local-capture-fidelity.md](B6-local-capture-fidelity.md) | none (field report) | B1 × B3 | **Deployed** (`bad9e0c`): L0, L1, L2, L4; L5/L6 open |
| B7 | [B7-echo-free-capture.md](B7-echo-free-capture.md) | none (safeguard report + owner constraint: MCP only) | replaces B1 §2/§4/§6 | **Deployed** (`bad9e0c`, `96acd6c`): path E, E0–E5, E2b; P1-15 reply contract uncommitted; E6/E6b/E7 open |

The original plan `.md` files are **not in the repo**. Copy them to `docs/plans/` (unchanged) so the section numbers cited here can be resolved. Until then they exist only in the chat that produced this folder.

## Status legend (used in every branch file)

| Mark | Meaning |
|---|---|
| ✅ | Implemented as planned (evidence given) |
| ◐ | Partially implemented, or implemented with a deviation (explained) |
| ❌ | Not implemented |
| 🔀 | Superseded: replaced by a later branch/decision (points to it) |
| ⏸ | Deferred / gated on the user (an external gate) |
| ⚠ | Implemented, but a defect or regression was found (links to `../03-issues.md`) |
| ? | Not verified yet. Do not rely on it |

## Timeline entry types

`ADDED` new decision or feature · `REPLACED` decision swapped for another · `REVISED` same decision, changed detail · `BRANCHED` new plan or branch split off · `DIVERGED` code differs from plan without a recorded decision · `DEFERRED` · `REGRESSED` worked, then broke · `FIXED` · `FOUND` audit finding (no code change)

## How to keep this up to date (rules for any future session)

1. **Before** changing code in an area, open that branch file and check its decision row: you may be about to undo a deliberate replacement.
2. **After** the change, in the same commit:
   - update the decision row's status and evidence in the branch file;
   - add one line to that branch file's **Change log** (newest first);
   - add one row to [TIMELINE.md](TIMELINE.md) (newest at the bottom), giving the commit hash and the type.
3. If a change crosses branches (e.g. B5 work that fixes a B4 gap), log it under **each** affected branch and once in TIMELINE with both IDs (`B5→B4`).
4. A new plan document, or a large unplanned direction like B5, gets a new `B<n>` file and a `BRANCHED` entry in TIMELINE.
5. Never delete a superseded row. Mark it 🔀 and point to what replaced it; the history is the point.
6. Evidence must be `path:line` or a test name. Claims taken from `PROGRESSs.md` without re-checking get `?`.
