# Wave C — live report: digest truthfulness and capture-to-analysis

**Date:** 2026-11-12
**HEAD:** `86b14bb` (shared dirty tree preserved)
**Verdict:** the digest P0 is closed on the disposable fixture; capture, revision,
tail analysis and independent native-render comparison pass. **Subjective
listening was not performed**, and this is not a mix-quality or production run.

## 1. Delegation

- Worker A (digest path): `scripts/reaper_bridge.lua`,
  `src/reaper_audio/context/revision.py`, `tests/test_revision.py`,
  `tests/test_wave_c_digest.py` — completed.
- Worker B (capture/analysis path): `src/reaper_audio/daw/session.py`,
  `src/reaper_audio/analysis/artifacts.py`,
  `tests/test_wave_c_capture.py`, `tests/test_wave_c_analyze.py`,
  fixture update in `tests/test_daw_session.py` — completed.
- Hermes: integration, shared digest fixtures, full suite, live REAPER 7.84 run,
  independent render comparison, ledger/checklist.

No worker was active at the end. Exactly two were used.

## 2. Unit and contract results

```
PYTHONPATH=src $TEMP/reaper-audio-library-python313/python.exe \
  -m pytest -q tests
# 725 passed, 2 skipped in 14.76s

... -m pytest -q tests/test_revision.py tests/test_wave_c_digest.py \
  tests/test_wave_c_capture.py tests/test_wave_c_analyze.py \
  tests/test_wave_b_digest.py tests/test_wave_b_capture.py tests/test_daw_session.py
# 246 passed in 3.16s
```

`git diff --check` exits 0 (only CRLF conversion warnings).

## 3. Live fixture and identity

- Instance: REAPER `7.84/x64`, dedicated `-newinst`, opened exactly
  `work/live-test-waveb/bastard_edit_ogg.RPP`.
- Fixture RPP hash before/after:
  `d726793994a557c0da3792c1f3d9cbd53849bac7801e6e728cbf675c926faa1e`.
- Source RPP hash: same value; source media aggregate hash before/after:
  `a39fdddfe141cd4f1d71425403133cef160eaea020cb3bdc1047788fbcd6d074`.
- 11 ordinary tracks, 108 items, one master with 3 FX; no tracks/items added.
- Live gate: `2 passed in 2.21s` (capture + master/coverage).

## 4. Digest coverage

Live `project.digest` (93,882 bytes, 0.049 s):

```
coverage = {tracks, items, takes, fx, sends, tempo,
            master, automation, envelopes, project, fx_state} = all true
missing = []
evidence.scope = "full"
```

- `fields.tracks`: 11 rows; level/pan/mute/solo/folder, FX identity/order/
  enabled/offline/all normalized params, sends, hardware outputs.
- `fields.items`: 108 rows; position/length/start offset/playrate/mute/fades/
  take/source/loop/beat-attach/stretch markers.
- `fields.tempo`: 0 explicit markers (constant tempo); the tempo map envelope is
  under automation.
- `fields.master`: GUID, volume/pan/mute/solo, 3 FX, sends, hardware outputs.
- `fields.automation`: 11 track rows, 1 master tempo-map envelope, 108 items
  scanned, 0 FX envelopes, `complete=true`, `gaps=[]`.
- `fields.project`: 27 render/playback settings, `complete=true`, `unread=[]`.
- `fields.fx_state`: 12 rows (11 tracks + master), normalized `<FXCHAIN>` text,
  `complete=true`.

Python `digest_evidence` requires a payload for every true coverage name; a flag
without its payload is moved to `missing` and cannot yield `full`.

## 5. Live fingerprint mutations

Each mutation used `transaction.preview` → `transaction.apply` → digest → undo:

| Mutation | Fingerprint changed | Undo restored |
|---|---|---|
| Master volume 1.0 → 0.5 | yes | yes, exact fingerprint |
| Master FX enabled → false | yes | yes |
| Master FX parameter normalized 0.5 → 0.9 | yes | yes |
| Master tempo-map automation point added | yes | yes |

The fixture has no track/item/FX envelope points; those payload mutations are
covered by `tests/test_wave_c_digest.py`, not by a live track-envelope edit.

## 6. Independent native render

An independent REAPER render used the normal action `42230`, not
`project_capture_render`:

- bounds: custom 0–1 s; tail 250 ms; master mix; stereo; 48 kHz;
  `RENDER_ADDTOPROJ=0`.
- Captured master: 60,000 frames, 48 kHz, 2 ch, 1.25 s.
- Independent master: 60,000 frames, 48 kHz, 2 ch, 1.25 s.

Measurements (float64, both files):

```
max_abs_diff       = 0.0
diff_rms_dbfs      = -300.0 (float floor)
null_residual_db   = -280.26
correlation        = 1.0
reference_rms_dbfs = -19.74
```

The WAV file hashes differ only because of container headers; the sample arrays
are bit-identical. Render settings were snapshotted before and restored exactly
after: `RENDER_BOUNDSFLAG=1`, `RENDER_ENDPOS=311.3063217`,
`RENDER_TAILFLAG=0`, `RENDER_TAILMS=1000`, `RENDER_SRATE=44100`, strings
`RENDER_FORMAT` (original binary preset), `RENDER_FILE`/`RENDER_PATTERN` empty.

Retained result: `work/live-test-waveb/compare2/captured-master.wav` and
`work/live-test-waveb/compare2/independent-master.wav`.

## 7. Tail and analysis

- `analyze(..., tail_ms=250)` captures 1.25 s files but measures only the
  project section 0–1 s; the 1.25 s tail is reported separately and never leaks
  into the measured window.
- `report["capture"]`, `report["capture_time_window"]`, `report["tail_s"]` and
  per-source `analysis_window` preserve project window, file window and measured
  window (project start/end, file start/end, measured start/end, tail).
- `capture_mode="accessor"` with `tail_ms>0` fails before creating a workspace or
  rendering.
- `restore_report` is mandatory and factual; the old synthetic `{restored: true}`
  default is gone.

## 8. Artifact ownership

The live capture registered 12 files, hashed each byte stream, and removed only
manifest-owned temporary WAVs. The job directory was removed; no `rali-*` orphan
remained; no track/item was added. Unregistered files are preserved and reported
as cleanup errors where applicable.

## 9. Remaining gaps

| ID | Gap | Class |
|---|---|---|
| WC-01 | Subjective listening comparison was not performed. The independent render is sample-identical, so no audible difference is expected, but this is not a human listening result. | open |
| WC-02 | No same-song pre-mix reference; this fixture validates REAPER access, capture, restore and render equivalence only, not mix quality. | open |
| WC-03 | Live automation mutation exercised the master tempo-map envelope only; live track/item/FX envelope edits were not performed (none exist in the fixture). Unit tests cover their payload mutations. | open |
| WC-04 | `mcp_server.py` does not yet pass `tail_ms`/`channels`/`sample_rate`; the MCP surface is unchanged. | open |
| WC-05 | `capture_method` legacy string still coexists with `capture_method_typed`, documented and tested; no production reader consumes the typed value yet. | accepted |
| WC-06 | `Capture.missing` still speaks about captured assets; digest gaps live in `coverage.digest_missing`. | accepted |
| WC-07 | `fields.fx_state` hashes normalized RPP chain text; UI lines at nested container level are kept (conservative false positives, never false equality). | accepted |
| WC-08 | Digest was measured on 11 tracks/108 items only; scaling to very large projects is untested. 93.9 KB / 49 ms is the current fixture measurement. | open |
| WC-09 | Full digest is REAPER 7.84-only evidence; 7.85 API differences are not exercised. | open |

## 10. Smallest next safe wave

Wave D: persist and replay the digest/capture evidence in the context ledger
(append-only records with scoped `dep_set`, manifest-owned assets, and a
Tier-0/Tier-1 view), then wire the existing capture contract into the MCP surface
without adding genre recipes. Keep subjective listening and a same-song A/B as a
separate rights/dataset task.
