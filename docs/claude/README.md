# docs/claude — Claude's working notes on ThreadVault

Written 2026-10-05 after a full read of the repo (source, tests, docs, deploy configs, git history, `PROGRESSs.md`).
These notes are Claude's own understanding, meant to get a new session productive quickly and to track what to fix.
They are **not** a replacement for `docs/DEPLOYMENT.md` etc. — where they disagree with those docs, these notes describe what the *code and deploy config actually do*.

| File | Read it for |
|---|---|
| [01-project-understanding.md](01-project-understanding.md) | What ThreadVault is, architecture, module map, data model, protocol |
| [02-current-state.md](02-current-state.md) | What is actually deployed today, test status, doc-vs-reality drift, repo hygiene |
| [03-issues.md](03-issues.md) | Prioritised issue list (P0–P3) with evidence (file:line) and a proposed fix for each |
| [04-action-plan.md](04-action-plan.md) | Ordered next steps, verification commands, open questions for the owner |
| [branches/](branches/README.md) | One file per plan branch (B0 plan v2 → B1 reliability, B2 cross-device → B3 write path, B4 GitHub; B5 unplanned standalone deploy): each decision's status with evidence, what replaced it, a per-branch change log |
| [branches/TIMELINE.md](branches/TIMELINE.md) | One chronological log of every replaced / revised / branched decision across all branches. **Append a row with every change** |

Keep these files updated when things change; delete findings once fixed (git history keeps them).
