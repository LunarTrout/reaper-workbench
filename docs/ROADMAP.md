# Current engineering priorities

This page records the remaining implementation and validation gates. It is not
a mixing recipe. Confirmed behavior and evidence are in
the [handbook](HANDBOOK.md); the current model-facing contract is in the
[MCP guide](MCP-README-LLM.md).

## Implemented scope

- A Python library controls REAPER through its documented ReaScript interface
  and resident Lua bridge. The MCP server exposes 41 tools, 5 resource
  templates, and 3 static resources; the ReaScript catalog is discovered
  through capability lookup and structured search rather than one tool per API
  function.
- The model receives one full project panorama, in one response when it fits
  or as a complete integrity-checked page set when required. The acknowledged
  context token identifies the model's latest known state.
- Routine edits return a `change_update`: only added, changed, or removed facts
  with their new current values and stable IDs. The server retains the full
  operation journal internally. Explicit archived-capture comparisons and
  numerical audio-metric differences are separate operations.
- Audio capture, integrated measurements, reference comparisons, mix evidence,
  scoped queries, revision-bound edits, and candidate rendering are available
  through the current library/MCP surface. The MCP guide describes their
  semantics and limits.
- Context Contract v2 is implemented and accepted on a disposable bench:
  fingerprint-keyed measurement cache, per-(source, stage) freshness, coverage
  passports over sources and time, decision gates that only raise a class,
  structural tokens for edits, partial evidence (per-source documents plus a thin
  manifest, 0.19 % of the record), incremental rendering on by default
  (max |Δ| = 0.0043 dB reused vs fresh), a post-hoc change journal for raw
  calls, and user-owned work modes (strict/advisory/off) with a journal and a
  lock.

## Remaining release gates

1. Compare a live candidate render with a normal REAPER render using identical
   project, revision, interval, and render settings.
2. Exercise a complete edit cycle over live MCP: preview, apply, merge the
   returned state update, apply a related edit using its successor token, and
   undo with readback.
3. Verify that capture, analysis, and candidate render work while REAPER stays
   minimized and another application retains focus. Current code may restore
   and foreground REAPER.
4. Complete the live dependency-digest audit and verify which project, media,
   plugin, routing, and render changes invalidate audio evidence.
5. Verify audio-resource delivery with the intended MCP clients and models.
6. Expand isolated live coverage across FX, routing, MIDI, automation, save,
   track/item lifecycle, and failure recovery; then verify installation from a
   clean Windows environment.
7. Finish packaging/CI and dependency/file-license review before calling the
   repository release-ready.

These gates do not imply that all work must be performed in a fixed musical
sequence. They describe technical evidence needed to make the current interface
and its limits trustworthy.

## Project state and context

One mutable state document per project is the source of context. `capture`
writes map objects and audio references, `measure` upserts facts, `status`,
`freshness` and `coverage` read state, and `session(context)` builds the
agent-facing view from it.

Done: `ProjectState` (objects, interned fingerprints, facts,
current/stale/superseded, overlaps, token, changes_since); map v2 in state;
`session(context)` with live names/revision, `since` deltas and pages;
`session(state)` compact facts without renders; FX `set_params` with probed
defaults for effects and `first_seen` for instruments; append-only journal
with required `why` and basis; async per-source capture; media wake; bounded
state (about 445 KB on the test project, fingerprints stored once, superseded
rows keep lineage only).

Legacy audit: the evidence store is not a second source of context — it backs
citation tokens (`analyze.evidence`) and audio audit; panorama pages render an
archived capture for delivery receipts. Neither is read to answer project
context, which comes only from state. Fallbacks for archives written before
state exist solely for migration and are not used by a state-backed project.

State is bounded: fingerprints are interned once, superseded facts keep
lineage without payload, and change/history tails are capped. The test project
is about 389 KB; further reduction would drop measured values that queries may
still return, so it is intentionally not done.
