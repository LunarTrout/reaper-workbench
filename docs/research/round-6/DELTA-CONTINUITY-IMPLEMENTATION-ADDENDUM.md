# Blocking implementation addendum: panorama continuity

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


Date: 2026-11-13  
Status: Owner requirement; implementation blocker

## Required user-facing behavior

The model receives one complete, lossless logical panorama for a project revision. Once that full view is acknowledged, the model keeps it as its working context. Later project updates are delivered as exact, revision-bound factual deltas. The model can request a full panorama at any time. A full reload is required only when the base is stale, missing, ambiguous, too old to diff safely, or the owner explicitly requests it.

Do not silently replace the panorama with a reduced summary. Do not pretend an invalidated measurement is current. Do not rely on a Hermes-, Codex-, or client-specific memory hook.

## Audit finding that must be closed

The current page lifecycle can mint a receipt after all full-panorama pages have been served and their hashes match. However, the mutation gate currently binds that receipt to one exact capture revision. `changes_since` returns a delta but does not acknowledge it or mint a successor continuity token. After a mutation advances the project revision, this design can force the model to fetch and acknowledge every full-panorama page again before its next write. That contradicts the required one-full-load-then-deltas behavior.

## Required state machine

### 1. Initial full view

1. `panorama_open` returns a manifest, stable project identity/epoch, capture/revision, fact root, document hash, page count and page hashes, plus the first page.
2. The model fetches every remaining page for that exact handle and revision.
3. `panorama_ack` succeeds only if the server recorded every page fetch and all supplied hashes match. It returns a context token bound to project identity/epoch, revision, capture ID, fact root, schema and document hash.
4. A partial, mixed-revision, mixed-project or stale page set never establishes complete context.

### 2. Deltas and continuity

1. A delta request names the current context token and the exact acknowledged base capture/revision/fact root.
2. The server returns a deterministic delta identity and hash, exact target capture/revision/root, changed facts, additions, removals, and dependency invalidations. The payload must identify both what changed and what is now unknown/stale.
3. If the delta is paged, every delta page must be fetched and hash-checked just like a full panorama. An oversized or unrepresentable delta returns `refresh_required`; it must never be silently shortened.
4. A delta-acknowledgement step verifies that the exact delta was served from the token's base and that the supplied hash matches. It then returns a successor context token bound to the target revision/root. Repeating the same acknowledgement is idempotent.
5. A missing, stale, wrong-project, wrong-base, wrong-epoch or unacknowledged delta cannot advance the context token. The response states exactly whether to retry a page, recapture, or request a full refresh.
6. `panorama_refresh` always remains available and starts a new full-view handle. It is the recovery path when continuity cannot be proven.

### 3. Project mutations

1. Every write continues to require an expected REAPER revision and the latest valid context token. No low-level ReaScript call, Action, save operation or alternate mutation path may bypass the gate.
2. After a successful write, the server returns the exact before/after revisions and verified readback for touched state. It must identify affected evidence domains and invalidate dependent audio measurements when the write can alter signal, arrangement, routing or timing.
3. The model must receive and acknowledge that factual operation delta before the token advances. For signal-affecting changes, the delta must say that prior audio evidence is stale until a new capture is analyzed; it must not fabricate new measurements.
4. A new audio capture produces a revision-bound capture delta from the last acknowledged base. After its delta is served and acknowledged, the model can continue without replaying unchanged panorama pages.
5. An unknown generic API/Action effect must be conservatively marked as an unknown/invalidation for the affected scope; when the affected scope cannot be established, require refresh before another dependent write.

### 4. Whole-project analysis and claims

Project-wide diagnosis, comparison, or claims about the complete mix require the latest acknowledged context token too. A tool may compute scoped evidence before that point, but its response must state its exact target/window and cannot be presented as a whole-project conclusion. A partial page set, unacknowledged delta, stale token or invalidated capture must block or qualify project-wide conclusions. Capture generation itself may run without a complete-view token so the model can obtain the panorama; the act of creating a capture does not imply that the model has received or understood it.

## Acceptance tests

- Initial full panorama: all pages fetched, exact hashes acknowledged, token bound to project epoch/revision/root.
- A partial page set, wrong hash, wrong project, wrong epoch and stale revision cannot mint or advance a token.
- Two consecutive edit → verified state delta → acknowledgement cycles work without fetching full-panorama pages again.
- A signal-affecting edit invalidates relevant captured audio facts; after a new capture, an exact delta updates those facts without resending unchanged pages.
- A stale base, missing capture, delta overflow, unknown generic API side effect, or counter-reset/project-reopen epoch mismatch fails closed with a concrete recovery call.
- Duplicate page and delta acknowledgements are idempotent; an old token cannot be replayed for a different revision or project.
- A mutation is blocked until the latest delta is acknowledged. The gate covers `reaper_edit`, generic API calls, Actions, save/save-as and every other write path.
- Whole-project analysis/claims are blocked or explicitly scoped until the current panorama/delta chain is acknowledged; missing pages cannot be bypassed by calling a parallel analysis tool.
- Tests prove tool-flow delivery and server state transitions only. Documentation must say they cannot prove that the model attended to or semantically understood every fact; that is measured in the clean-session evaluation.

## Documentation and evaluation

Update both model guides and both repository READMEs to explain the full → delta → full-refresh lifecycle, exact recovery conditions, revision-bound mutation rule, invalidation semantics and the difference between server delivery acknowledgement and model comprehension. Keep advice flexible: no forced mixing order or genre recipe.

Before declaring the MCP complete, run the lifecycle tests, the full suite, a stock-client pagination check, and a clean Hermes session on a disposable project. Log the received page/delta hashes, revisions, tool calls, readbacks and any omitted context. Do not call the system production-ready until those gates pass.

The clean-session evaluator is valid only if its session metadata confirms the requested `deepseek/deepseek-v4.1-flash-1017` model, RouterAI provider and Ultra reasoning before its first MCP call. Set these explicitly at session creation; never rely on the desktop/default model. A delta experiment must put its base and target captures in the same archive root. A new archive without the base capture tests `stale_base`, not normal delta continuity. Record the session ID and archive/capture IDs so neither condition can be mistaken for a successful evaluation.
