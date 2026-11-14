# Wave C — digest truthfulness and capture-to-analysis

## Goal

Continue from the current dirty worktree. Wave B is integrated and its unit suite
passes, but it is **not** a production-ready or live-verified library. The next
wave must make revision evidence truthful, connect captured audio to analysis
without losing its time window, and run the live gate on a disposable REAPER
project.

## Delegation limit and ownership

- Use **at most two subagents total at once**. Wave B used one completed worker
  (`deleg_35f0f7e1`, task 0); there are currently zero active workers. Do not
  report zero Wave B workers used.
- Worker A owns only the native digest path and its tests:
  `scripts/reaper_bridge.lua`, `src/reaper_audio/context/revision.py`,
  `tests/test_revision.py`, and new digest contract tests.
- Worker B owns only Python capture/analysis integration and its tests:
  `src/reaper_audio/daw/session.py`, `src/reaper_audio/analysis/artifacts.py`,
  relevant audio context models, and new capture/analyze tests.
- Hermes owns integration, cross-lane review, the decision ledger/checklist, the
  complete test run, and the live REAPER run. No other agent may edit another
  worker's files. If using only one worker is enough, do so.
- Preserve all pre-existing dirty files and user data. Do not reset, checkout,
  clean, or overwrite unrelated changes. Do not touch source OGGs or the source
  test project.

## P0 — make revision evidence fail closed

Current behavior established by Wave B review:

- `project.digest` enumerates only ordinary tracks. It does not hash master
  volume/pan/FX/send state.
- It reports `coverage.master=false` and `missing=[..., "master"]`; envelopes
  are also missing. Therefore the current live digest must remain coarse.
- `DIGEST_REQUIRED_FIELDS` does not include `master` or `envelopes`, and
  `digest_evidence` trusts the bridge's coverage flags without verifying that a
  true coverage group has a matching payload. A synthetic digest can claim
  full scope without master data.
- `same_content` intentionally ignores the change counter and compares the
  fingerprint. Do not make the counter a substitute for content evidence.

Required work:

1. Enumerate the sound-affecting state needed for a truthful project revision:
   master track state; ordinary track state; routing/sends including relevant
   master/hardware outputs; FX order, identity, enabled/offline state and full
   serialized state or a proven complete parameter representation; item/take
   state; tempo map; track, master, item and FX automation; and project-wide
   render/playback settings that affect the captured result.
2. Use REAPER's documented ReaScript APIs only. Verify each chosen API in the
   checked-in API catalog and against the installed REAPER 7.84 runtime before
   relying on it. The catalog source is 7.85, so do not assume newer calls exist
   in the installed version.
3. Hash the master explicitly or keep `coverage.master=false` and add `master`
   to `missing`. Do the same for any incomplete automation, FX-state, routing,
   or project-setting group. Missing evidence must force `fingerprint_scope` to
   `coarse`; `same_content` must return false for coarse revisions.
4. Update the Python digest contract so each required `coverage` name maps to a
   required field payload. A true flag with a missing/empty required payload
   must not produce `scope="full"`. Add `master` and automation coverage to the
   required set only when the payload is actually represented and tested.
5. Add regression tests proving that absence of master or envelope evidence can
   never yield a full revision; changing master volume, pan, FX identity/order,
   enabled state, parameters/state, sends, or automation changes the content
   fingerprint; equal fully covered content matches; and coarse content never
   matches. Include opaque plugin state if the native API exposes it. If it
   cannot be captured completely, report that gap and keep the revision coarse.

Do not remove the live-test `xfail` or claim the master gate is closed until the
Lua payload and Python contract agree and a live REAPER run proves the behavior.

## Python capture-to-analysis path

Close Wave B gaps WB-03 through WB-07 where feasible:

- Either make tail-bearing captures analyzable with their project/file windows
  preserved through every analysis result, or explicitly reject them before
  rendering. Do not silently analyze a different interval.
- Ensure `tail_s` and per-asset windows are passed to metrics and comparison
  consumers. Keep project-time measurements distinct from rendered-file time.
- Require the bridge's actual `restore_report`; do not synthesize
  `{restored: true}` when a field is absent.
- Keep artifact ownership exact: hash registered files, remove only manifest-
  owned temporary capture data, preserve unrelated files, and leave no stems on
  the REAPER playlist.
- Preserve typed capture metadata and revision provenance end-to-end; document
  and test any temporary legacy compatibility rather than silently duplicating
  fields forever.

## Live REAPER acceptance

The existing gate is `tests/test_wave_b_live_capture.py`; its checklist is
`docs/research/round-3/WAVE-B-LIVE-CHECKLIST.md`. Run it only after the digest
P0 is fixed or explicitly fail-closed.

Disposable project copy already prepared inside the ignored work directory:

`work/live-test-waveb/bastard_edit_ogg.RPP`

It is a copy of the downloaded 11-track REAPER test project; the source RPP hash
at copy time was `D726793994A557C0DA3792C1F3D9CBD53849BAC7801E6E728CBF675C926FAA1E`.
The copy contains 60 OGG files. The copied RPP still references the original
OGG paths, so treat those source files as read-only. Save/edit only this copied
RPP and temporary output directories. Verify the source RPP and source media
hashes are unchanged after the run.

Live acceptance must prove all of the following:

1. A dedicated REAPER 7.84 instance opens exactly the disposable copy; never
   attach to or save a user project.
2. The Lua bridge is running through the normal REAPER/ReaScript mechanism.
3. `project.digest` reports exact coverage/missing status. Mutating and undoing
   master FX/state and automation in the disposable copy changes/restores the
   expected fingerprint; no incomplete state is labelled full.
4. Capture returns one post-FX stem per ordinary track and exactly one master
   mix for the requested interval, with correct BPM/time context, sample rate,
   channels, frame counts, hashes, and optional tail window.
5. Render settings, solo/mute state, track/item counts, project state, and file
   ownership are read back after capture; restore failures fail the gate.
6. Compare the captured master against an **independent normal native render**
   of the same project, interval, FX state, sample rate, channel layout, tail,
   and render settings. Report numerical tolerances and a listening comparison;
   do not infer equivalence from matching metadata or unit tests.
7. Verify temporary artifacts are removed and no track/item was added to the
   project. Retain only the original fixture and the one approved final result;
   do not accumulate intermediate stems.

The project has no matching pre-mix reference, so it can validate REAPER access,
capture, restore, and render equivalence but **cannot** serve as the same-song
mix A/B. Keep that A/B as a separate dataset/rights task; never include source
stems or copyrighted reference audio in the public repository.

## Verification and report

Run focused tests for both ownership areas, then the full suite with the Python
3.13 environment used in Wave B. Record exact commands and counts. Run
`git diff --check`; inspect the entire source diff and preserve prior changes.

The report must state:

- actual subagent count (started, completed, active), never infer it from the
  current active-worker list alone;
- changed files and per-owner scope;
- focused and full test results;
- live fixture path and exact live gate outcome, or the precise blocker;
- digest groups covered, missing, and still coarse;
- master/effect/automation fingerprint behavior;
- independent render-comparison measurements and listening result;
- remaining P0/P1 gaps and the next smallest safe wave.

Do not call the library complete, production-ready, or audio-equivalent while
any P0 remains open or the live gate has not passed.
