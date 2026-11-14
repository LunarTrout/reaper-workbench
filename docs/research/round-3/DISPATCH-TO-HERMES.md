# Hermes dispatch — R3 implementation design

Use the existing user-visible Hermes session titled “REAPER Workbench — 6 research agents” if it is resumable. Preserve the requested DeepSeek V4.1 Flash 1017 model with Ultra reasoning. If the session cannot be resumed, do not silently create a parallel chat: report the technical reason and use the closest established visible session only after verifying its identity.

The R3 assignment is a separate design phase after the six R2 research reports. It must deliver a coding blueprint that lets the owner begin integrating the library after this phase. Read docs/research/round-3/00-IMPLEMENTATION-READINESS-CONTRACT.md and all six R3 briefs, then all six final R2 reports and the real current code/docs/tests. Check git HEAD/status; preserve all shared dirty changes.

Delegate exactly six tasks, one per R3-01…R3-06. Do not create a seventh researcher. Each agent must read all six R3 briefs and R2 reports so that every owner knows the other five inputs/outputs. The project parent integrates the plans. This is not permission to implement code: no source/test/config/docs edits outside each designated report and the two parent synthesis docs; do not run tests, REAPER, render or inference.

Each agent writes only its own work/research/round-3/reports/R3-0N-*.md. Use a single final file per topic; do not create part/v2/final alternatives. If a write conflicts, read current contents and merge into the owned report; do not overwrite sibling files. Draft all six reports, then make each agent cross-read the other five, record dependency/conflict decisions in its report, and mark STATUS: FINAL only after the review.

The parent writes docs/research/round-3/IMPLEMENTATION-BLUEPRINT.md and DECISION-LEDGER.md. Blueprint has a canonical architecture and shared types, a repo map citing current symbols, producer/consumer contracts, file-level coding backlog with prerequisites, staged build order, test design and quantitative exit gates, reversibility/version lifecycle, explicit empirical blockers, and the smallest end-to-end vertical slice. Ledger classifies decisions as accepted, proposed, owner decision or empirically blocked, with evidence/owner/next resolving action. Reconcile contradictory documentation from actual code. It must finish with: what can be implemented first, what depends on live capture/V100/human evaluation, and how work can be split into safe coding increments.

Do not claim R3 completes the product. Its successful exit is a complete, internally consistent, code-ready plan and six cross-reviewed reports, after which implementation can begin in a later phase.

