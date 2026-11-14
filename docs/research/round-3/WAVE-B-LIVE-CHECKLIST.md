# Wave B — live capture checklist (lane 6 of 6)

> **Wave C update (2026-11-12):** the P0 master/automation gap is closed. The
> current bridge reports a fully covered digest (`coverage.master=true`,
> `automation/envelopes/project/fx_state=true`, `missing=[]`), the live gate is
> **2 passed**, and an independent native master render is sample-identical to
> the captured master (60 000 frames, 48 kHz stereo, 1.25 s, max abs diff 0.0,
> null residual ≈ −280 dBFS). The historical Wave B numbers and G10/G11 notes
> below are retained as the pre-Wave-C record; see
> `WAVE-C-LIVE-REPORT.md` for the current evidence. Subjective listening was not
> performed.

**Baseline:** HEAD `86b14bb`; the pre-existing dirty tree (`docs/AI-MIX-SURFACE.md`,
`docs/TZ.md`, `scripts/reaper_bridge.lua`, the Wave B source files and untracked
`docs/research/`) was preserved.
**Lane scope:** independent verification only. No source file, no existing test and no
other lane's file was edited. Owned artifacts: this checklist and
`tests/test_wave_b_capture.py`, `tests/test_wave_b_digest.py`,
`tests/test_wave_b_bridge_contract.py`, `tests/test_wave_b_live_capture.py`.

**Verdict:** the fake-bridge/unit gates are green (**85 passed, 2 skipped**; full suite
**601 passed, 2 skipped**). The live gate is **blocked**: no REAPER instance is attached
to this session, and `tests/test_wave_b_live_capture.py` is skipped unless explicitly
armed. Review found one **P0 truthfulness gap** (the master track is absent from
`project.digest`), three P1s and two P2s — all recorded in §7. None were fixed in this
lane because the owning files belong to other lanes.

---

## 1. What is already proven without REAPER

| File | Items | What it pins |
|---|---|---|
| `tests/test_wave_b_capture.py` | 7 | `ProjectSession.capture_audio` yields a typed `Capture`; two `AudioAsset`s with `ast_sha256_<sha>` ids, `master_mix_post_fx` / `track_stem_post_fx`, `native_render`, `source_kind=unknown`, `channel_view=native`, 48 kHz / 60 000 frames / 1.25 s; project window `0..1`, file window `0..1.25`, `capture.tail_s=0.25`; file hash == asset hash; manifest cleanup removes exactly the registered files; an unregistered file is preserved and surfaced as `ArtifactCleanupError`; track or item growth during the render raises `BridgeUnavailable`; a partial digest yields `warnings=("revision_digest_partial",)` and `fingerprint_scope="coarse"`; the accessor path is `take_sum_pre_fx` + `audio_accessor` for tracks and `master_mix_post_fx` + `native_render` for the master. |
| `tests/test_wave_b_digest.py` | 50 | A full digest reports `scope="full"`; each of 30 sound-affecting mutations (track volume/pan/mute/solo/folder depth, FX param/index/enabled/offline/name, send volume/pan/mode/destination, item position/length/offset/playrate/mute/fades/take/source/loop/beat-attach/stretch marker, tempo bpm/numerator/denominator/linear) changes the fingerprint; selection/transport/name/change_count/path/extra keys do not; `missing=["envelopes"]` makes a digest coarse; two identical coarse digests never satisfy `same_content`; a coarse scope never matches even when the fingerprint is equal; empty `fields` is coarse; `revision_from_digest` keeps the caller's counter and the digest scope; malformed digests are rejected. |
| `tests/test_wave_b_bridge_contract.py` | 28 | `ReaperBridge.capture_render` accepts a complete response and forwards the request; refuses a missing `render_settings_used`/`channels`/`sample_rate`/`tail_s`/`restore_report`; refuses a wrong channel layout, an unapplied tail, an unrestored project, `RENDER_ADDTOPROJ != 0` and a wrong `RENDER_BOUNDSFLAG`; accepts the section bounds it requested. `ReaperBridge.project_digest` accepts the frozen shape and rejects 13 malformed shapes. |
| `tests/test_wave_b_live_capture.py` | 2 (skipped) | The live procedure below, encoded as a test. Never opens, saves or closes a project: it verifies that the bridge's open project *is* the fixture copy named by `REAPER_WORKBENCH_FIXTURE`. |

Commands and exact results (Python 3.13 temp env, `PYTHONPATH=src`):

```
python -m pytest -q tests/test_wave_b_capture.py \
  tests/test_wave_b_digest.py tests/test_wave_b_bridge_contract.py \
  tests/test_wave_b_live_capture.py
# 85 passed, 2 skipped in 1.41s

python -m pytest -q tests
# 601 passed, 2 skipped in 12.80s
```

The baseline before this lane was **516 passed**; 85 tests were added and no existing
test was modified.

---

## 2. Live fixture

Use a **disposable copy**, never the user's working project.

Required fixture properties (the test asserts most of them indirectly):

- REAPER 7.84 or newer with the resident `scripts/reaper_bridge.lua` loaded; the bridge
  must answer `project.digest` and `project.capture_render` (a Wave A bridge will fail
  the contract and be marked legacy).
- At least 2 audio tracks with media at **0.0–1.0 s** (known tones or dry stems are
  easiest to verify), project sample rate 48 kHz.
- One track with a non-neutral fader/pan, one muted track, one track with a send to a
  bus, one track with at least one FX whose parameter is not at default.
- At least one master FX (limiter or EQ).
- The project must have **no unsaved changes** when captured (the gate does not save).
- The fixture copy should live outside the repository, e.g.
  `%TEMP%\wave-b-live\WAVE-B-LIVE-FIXTURE.rpp`.

Copy in git-bash:

```bash
mkdir -p "$LOCALAPPDATA/Temp/wave-b-live"
cp "/path/to/source-project.rpp" "$LOCALAPPDATA/Temp/wave-b-live/WAVE-B-LIVE-FIXTURE.rpp"
```

A project copy that references media by absolute path is fine; if media is missing,
REAPER opens with silent items and the hash assertions still pass, but G11 (audible
comparison) cannot.

---

## 3. Procedure

1. **Prepare a dedicated REAPER instance.** Close other projects or use a separate
   REAPER process. Open the fixture copy manually (`File → Open project`). Do not use
   the real project tab.
2. **Confirm the bridge sees the copy.** The resident bridge must report the copy as an
   open project, not the live project:
   ```bash
   cd "<repo>"
   PYTHONPATH=src $TEMP/reaper-audio-library-python313/python.exe - <<'PY'
   from reaper_audio.reaper.bridge_client import ReaperBridge
   bridge = ReaperBridge()
   print(bridge.health()["reaper_version"])
   for entry in bridge.list_projects()["projects"]:
       print(entry["revision"]["project_id"], entry["revision"]["path"], entry["is_active"])
   PY
   ```
   Pass condition: exactly one entry has the fixture copy's absolute path.
3. **Arm the gate** (values are per-shell, not persistent):
   ```bash
   export REAPER_WORKBENCH_LIVE=1
   export REAPER_WORKBENCH_FIXTURE="$TEMP/wave-b-live/WAVE-B-LIVE-FIXTURE.rpp"
   ```
4. **Run the live test alone** (never as part of the default suite):
   ```bash
   PYTHONPATH=src $TEMP/reaper-audio-library-python313/python.exe \
     -m pytest -q -s tests/test_wave_b_live_capture.py
   ```
   Expected today: **1 passed, 1 xfailed** (the xfail is G10 below). `-s` prints the
   capture id, digest scope/coverage, and every asset hash for the report.
5. **Run the manual independent render check** (G11) in a scratch tab; see §4.
6. **Clean up:** delete the fixture copy and `%TEMP%\wave-b-live`, unset both env
   variables, and leave the user's real project untouched.

If the fixture is not open, the test **fails** with instructions instead of skipping:
arming the gate is an explicit statement that a disposable project is ready.

---

## 4. Gates

| # | Gate | Pass condition | Where |
|---|---|---|---|
| G1 | Fixture identity | The bridge's open project path equals `REAPER_WORKBENCH_FIXTURE`; no other project is captured. | test |
| G2 | Capture completeness | `capture.status == "complete"`; `len(capture.assets) == track_count + 1`; one master asset exists. | test |
| G3 | Stage / method | Every non-master asset is `track_stem_post_fx` + `native_render`; the master is `master_mix_post_fx` + `native_render`; `source_kind == "unknown"`; `channel_view == "native"` for stereo, `mono_sum` for mono. | test |
| G4 | Content identity | `sha256(file bytes) == asset.sha256 == asset_id` suffix; `asset.bytes == file size`. | test |
| G5 | Windows and tail | Project window `0.0–1.0`; file window `0.0–1.25` (±1 sample); `capture.tail_s == 0.25`; `time_window.requested_section == {0.0, 1.0}`. | test |
| G6 | Restore and settings | `restore_report.restored is True` and no failures; `RENDER_ADDTOPROJ == 0`; `RENDER_TAILMS == 250`; `project.render_settings.read` before/after equal on `render_settings`, `render_targets`, `dirty`. | test |
| G7 | No project growth | Track GUID list and item GUID list unchanged; digest fingerprint before == after (a render must not change a measured sound-affecting field). | test |
| G8 | Cleanup | Job directory removed; `removed_files` == asset count; no preserved paths; no `rali-<job>-*.wav` orphans in the artifact root. | test |
| G9 | Digest disclosure | `capture.coverage.digest_missing == digest["missing"]`; current Lua always reports `["envelopes"]`, so expect `revision_digest_partial` in `warnings` and `fingerprint_scope == "coarse"`. | test |
| G10 | **Master disclosure** | `digest["coverage"]["master"] is True` and `"master" not in digest["missing"]`. **Closed by Wave C**; live digest is full. | test |
| G11 | Independent render | Render the same 0–1 s window, tail 250 ms, identical channels/rate/settings; compare duration, per-asset peak/RMS and a null test. **Wave C numerical pass:** max abs diff 0.0, null residual ≈ −280 dBFS, correlation 1.0; subjective listening not performed. | manual |

**Fail rule:** any G1–G9 failure blocks the Wave B live claim. G10 failing is expected
today and must be reported as the P0; it must not be silently waived. G11 is required
before claiming audio equivalence or "same audio" — none of the unit tests prove it.

---

## 5. What the live run will show today

- G9: every live capture is `coarse` with `revision_digest_partial`, because the Lua
  digest hard-codes `missing = ["envelopes"]` (`scripts/reaper_bridge.lua:675-676`).
  `same_content(revision_before, revision_after)` is therefore **false on every live
  capture**; the gate must compare `digest_evidence(...).fingerprint` instead (the test
  does). Do not claim full revision freshness in the Wave B exit report.
- G10: `xfail`. The digest has no master row and no `master` coverage/missing entry.
- G2–G8 should pass on a fixture that meets §2. If G6 fails, inspect
  `restore_report.failures` and `project.render_settings.read`; do not re-run against
  the user's project.

---

## 6. Blocked status

- No REAPER instance is attached to this environment. **No live render, no audio
  comparison and no listener/model check was performed by this lane.**
- The live test is skipped by default; the numbers in this document are fake-bridge
  results only.
- The independent-render gate (G11) is manual by design; it cannot be asserted from the
  current unit suite.

---

## 7. Review findings

### 7.1 P0 — the master track is outside `project.digest`

`project_digest` iterates `for index = 0, track_count - 1 do` over `reaper.CountTracks`
(`scripts/reaper_bridge.lua:553`, `project_digest` starts at 547). REAPER's
`CountTracks` excludes the master, so:

- `fields.tracks` contains only non-master tracks (`reaper_bridge.lua:590-599`);
- `coverage` has `tracks/items/takes/fx/sends/tempo/envelopes` but **no `master`**
  (`reaper_bridge.lua:682-683`);
- `missing` contains only `"envelopes"` (`reaper_bridge.lua:675-676`).

Master volume, pan, mute/solo, master FX chain and master sends are sound-affecting but
are not hashed and are not disclosed as missing. Consequences:

1. A master-only change (fader, master limiter, master FX parameter) does not change the
   `digest_evidence` fingerprint. Since every live digest is also `coarse` (G9), the only
   cross-capture content check available today is fingerprint equality — and it passes
   while the master has moved. `same_content` is false for a different reason (the scope
   is coarse), not because the master was measured.
2. `Capture.coverage.digest` exposes the same incomplete coverage, and
   `coverage.digest_missing` lists only `envelopes` (`session.py:735-740`); a consumer
   sees no master gap anywhere.
3. `_structure_evidence` compares only non-master track GUIDs (`session.py:237-264`), so
   a master replacement between captures is invisible; a swap *during* a capture is
   caught only because `context_keys` includes `"master"` (`session.py:560-563`).
4. If envelopes were ever covered and `missing` emptied, `digest_evidence` would report
   `"full"` while the master is unmeasured — a false full-content claim.

Recommended fix (owner: lane 1 Lua + lane 5 reader): either append a master row
(`guid`, volume, pan, mute, solo, fx, sends, `"is_master": true`) to `fields.tracks`, or
add `coverage.master = false` plus `missing = ["master", "envelopes"]`. Because
`coverage` is part of the hashed payload (`context/revision.py:427-434`), adding a key
changes every fingerprint; decide explicitly whether that is a `digest_version` bump or
an accepted invalidation. Do not fix this in lane 6.

### 7.2 P1 — `coverage` is a producer claim, not checked against `fields`

`digest_evidence` calls a digest `"full"` when `missing` is empty, the required coverage
names are all true, and `fields` is non-empty (`context/revision.py:424-426`). It never
checks that a covered group has a corresponding field group. Probe:

```python
digest = {"digest_version": 1, "change_count": 3,
          "coverage": {k: True for k in
                       ("tracks", "items", "takes", "fx", "sends", "tempo")},
          "missing": [], "fields": {"tracks": []}}
# digest_evidence("reaper:probe", digest).scope == "full"
```

The Lua digest also claims `takes`, `fx` and `sends` covered while `fields` has no
top-level groups by those names: fx/sends are nested per track and takes are a
`take_guid` inside item rows. The data is hashed, but the claim is not independently
verifiable, and a producer can reach `"full"` with only `tracks` present. Recommendation:
map each covered name to its field path (`fx → fields.tracks[].fx`,
`sends → fields.tracks[].sends`, `takes → fields.items[].take_guid`) and require
evidence for every covered name, or document coverage as advisory and stop deriving
trust from it alone.

### 7.3 P1 — accessor capture cannot honour a tail

`capture_audio(..., capture_mode="accessor", tail_ms>0)` is inconsistent: the session
requires every file to be `section.duration_s + tail_ms/1000` (`session.py:474-487`),
while Lua `project_sample_tracks` writes exactly `floor((end_s - start_s) * rate + 0.5)`
frames with no tail (`scripts/reaper_bridge.lua:3187`). The combination fails
closed with "REAPER render does not cover the requested project section"; the test
`test_capture_audio_rejects_accessor_tail_it_cannot_honour` pins it. Recommendation:
pass the tail into `project.sample_tracks` and extend the accessor WAV, or reject the
combination up front with `ValidationError` instead of a late bridge error.

### 7.4 P1 — `capture.missing` cannot express digest gaps; partial status is unreachable

`session.py:688-689` puts digest gaps into `coverage["digest_missing"]`, while
`missing` (`session.py:741`) carries those names only when captured assets are
incomplete. But an incomplete render is refused earlier: `describe_track`
(`session.py:642-650`) raises `BridgeUnavailable` for any context track without a
rendered file, so a yielded `Capture` always has `status="complete"` and `missing == ()`.
A consumer that reads only `capture.missing` therefore sees "nothing missing" even when
the digest is partial. Recommendation: either list missing source GUIDs in `missing`
(and keep digest names separate), or rename to `missing_assets`, or remove the
unreachable partial path.

### 7.5 P1 — tail captures have no consumer

`capture_audio` can request `tail_ms > 0`, and the `Capture` window records the
distinction honestly (`project_end=1.0`, `file_end=1.25`). But `analyze` has no tail
parameter and `_load_rendered_audio` requires `decoded.duration_s == section.duration_s`
within one sample (`session.py:97-107`), so a tail-extended file cannot be loaded by any
repo path. The tail is currently write-only. Recommendation: add a tail-aware loader
that trims or measures the tail, or restrict `tail_ms` to listen/export paths.

### 7.6 P2 — an unverified restore is recorded as restored

`session.py:728-729` defaults `restore_report` to `{"restored": True, "failures": []}`
when the response omits it. `ReaperBridge.capture_render` rejects a missing
`restore_report` (`bridge_client.py:572-581`) and the session adds
`legacy_bridge_contract`, so this is reachable only through a non-`ReaperBridge` double —
but the stored value is still an optimistic claim. Recommendation: default to
`{"restored": None, "failures": []}` or omit the field.

### 7.7 P2 — two capture-method vocabularies

`session.py:662` writes `capture_method_typed` beside the legacy `capture_method`
string (`session.py:668-681`). The values agree today (`native_render` ↔
`reaper_native_offline_render`, `audio_accessor` ↔ `reaper_audio_accessor`) and no
production reader consumes the typed field yet, which is exactly the drift risk the two
vocabularies create. Recommendation: mark the legacy field deprecated and name the
migration path.

### 7.8 Ownership

No ownership violation found. `git status`/`git diff` show only the declared Wave B
files plus the two documents that were already dirty at baseline. This lane added only
the four test files and this checklist. `tests/test_bridge_client.py` was extended
(additively) by the bridge lane, not by this one.

---

## 8. Evidence to attach to the Wave B exit report

- `reaper_version`, bridge session id, fixture path, `project_id`.
- `capture_id`, per-asset `source_guid`/`sha256`/`stage`/`method`/`channel_view`.
- `capture.coverage` (including `digest` and `digest_missing`), `capture.warnings`,
  `fingerprint_scope` before/after, and the digest fingerprint equality result.
- `restore_report`, `RENDER_ADDTOPROJ`, `RENDER_TAILMS`, and the before/after
  `project.render_settings.read`.
- Track/item GUID lists before and after; cleanup report.
- G11 comparison numbers (duration, peak/RMS deltas, null-test residual) or an explicit
  "not run".
