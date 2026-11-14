# Wave D — same-song metal-mix benchmark: evidence report

**Status: experiment, not a release certification.** The first autonomous Hermes mix is
frozen and hashed. No professional reference audio was used before the freeze. The
model did **not** hear any audio; all judgements in this report come from measured
signals and project state. Subjective listening is still pending.

Date: 2026-11-12. REAPER: 7.84/x64. Workspace:
`<repo>`.

---

## 1. What is and is not proven

Proven:

- The current checkout's MCP server loads from `src/reaper_audio/mcp_server.py`,
  advertises **19 tools and 5 resource templates**, and answered live project,
  analyze and timeline calls against REAPER 7.84.
- A 41-track project (34 source tracks + 7 aux buses) was built from the official
  HiMMP `In Solitude` multitrack, without editing the source files.
- Bus routing, gain staging, EQ, compression, reverb/delay, master processing and
  master gain were changed through revision-bound MCP edit transactions.
- One final project and one full-song master were written, hashed and preserved.
- The frozen master has measured loudness, peak, dynamics, stereo and spectral
  descriptors, and a post-freeze objective comparison against nine loudness-matched
  HiMMP masters is recorded.
- Full test suite: **760 passed, 2 skipped** (live tests skipped by default). Focused
  Wave D MCP tests: **54 passed**.

Not proven:

- That the mix sounds good, is musically appropriate, or is competitive with the
  professional mixes. No human or model listened to it.
- That the 41-track project is a faithful reconstruction of any producer's session.
- Bus/role stem comparison: a receive-only REAPER bus renders silent as a normal
  track stem; the attempted bus capture was discarded. The MCP surface has no
  bus/solo-stem render. The four-role category-stem comparison is therefore
  **not completed**.
- That the MCP server can compare external reference WAVs; it cannot. The objective
  reference table is an offline analysis of files already staged after freeze, not an
  MCP tool.
- That the final WAV was produced through MCP. It was produced by the library's
  native REAPER capture path before the "MCP-only" instruction; it is a valid render
  but not evidence of an MCP render tool.

---

## 2. MCP connection and source identity

Experiment entry: `reaper-workbench-benchmark` in
`$LOCALAPPDATA\hermes\config.yaml`.

```
command: $TEMP/reaper-audio-library-python313/python.exe
args:    -m reaper_audio.mcp_server
env:     PYTHONPATH=<repo>/src
```

`hermes mcp test reaper-workbench-benchmark` connected (2.8 s) and discovered **19
tools**. The imported module path is
`<repo>\src\reaper_audio\mcp_server.py`.
The existing `reaper-mcp` entry was not changed. The benchmark entry is
experiment-only because it points at a temporary interpreter; remove it or replace
it with a stable install before ending the experiment.

Live session: `reaper_session` reported one open project, REAPER `7.84/x64`,
`HIMMP-In-Solitude.rpp`, revision change count 998. `reaper_project` reported 41
tracks, 34 items, tempo map 200 BPM → 240 BPM at bar 135 (160.8 s) → 200 BPM at bar
212/213 (237.8 s), length 300.52 s, master volume 0.8, three master FX
(`ReaEQ`, `ReaXcomp`, `ReaLimit`).

---

## 3. Dataset, licence, and input manifest

Official source: University of Huddersfield HiMMP archive, Box share
`https://huddersfield.app.box.com/s/8gren2ma4kesvf5vwip2axzz1v8sawur`.

- Song: `In Solitude`; multitrack folder `2) HiMMP Multitrack` (`169785155878`).
- 43 staged source files, 1.776 GB, with exact names, Box IDs, bytes and SHA-256 in
  `work/benchmark/himmp/manifests/SOURCE-MANIFEST.json`.
- Source inventory and role notes: `work/benchmark/himmp/manifests/INPUT-LIST.md`.
- Licence: **CC BY**. Credit: HiMMP (Heaviness in Metal Music Production), University
  of Huddersfield — Jan Herbst (PI) and Mark Mynett (Co-I). Full attribution and
  redistribution constraints: `work/benchmark/himmp/ATTRIBUTION.md`. Audio is not
  committed to Git or re-hosted.

Post-freeze reference set: 45 files, 3,478,452,816 bytes, in
`work/benchmark/himmp/reference/`, manifest
`work/benchmark/himmp/reference/REFERENCE-MANIFEST.json`.

- 9 loudness-matched masters: 8 commissioned producers + Research Team, all labelled
  `-10.4 LUFS`: Bogren, Exeter, Nolly Getgood, Middleton, Nordström, Odeholm, Otero,
  Scheps; Research Team kept separate.
- 36 role stems: 9 producers × drums, bass, guitars, vocals. Dataset role targets:
  drums/bass −26.5 LUFS, guitars −21.5 LUFS, vocals −22.0 LUFS. These are
  independently normalised; **relative role balance must not be inferred from them**.
- The eight producer mixes are a distribution of valid artistic choices, not one
  ground truth. Public materials disagree on whether a reference mix was supplied
  during the study; this report preserves that uncertainty.

---

## 4. The autonomous mix

The model received the user-level goal, project context, source inventory and
measured analysis. It did not receive reference audio, producer mix audio, or a
prescribed FX chain. Decisions were made from measured role balance, source peaks,
spectral/level evidence and project context.

Project construction and mix stages:

1. 34 source tracks imported at project time 0; only the base set, not the 6
   alternative takes; tempo map preserved.
2. Seven aux buses created: `DRUMS BUS`, `BASS BUS`, `GTR BUS`, `ORCH BUS`,
   `VOX BUS`, `VERB RETURN`, `DELAY RETURN`.
3. Stage 1b: per-source gain staging and routing to buses; conservative bus EQ and
   dynamics.
4. Stage 2: bus/master EQ, high-pass and dynamics; master limiter.
5. Stage 3: measured role-bus rebalance (drums/guitars were ~13 dB apart); tame
   close-mic peaks; FX-return high-pass.
6. Stage 4: bring drums, bass and orchestration forward after measured balance;
   vocals retained.
7. Stage 5: final master gain, measured at 10–15 s; no clipping.

Final mix state (selected):

- 41 tracks: 34 source + 7 buses; 34 items; length 300.52 s.
- Master: `ReaEQ` → `ReaXcomp` → `ReaLimit`, fader 0.80.
- Buses: `DRUMS BUS` 0.50, `BASS BUS` 1.45, `GTR BUS` 1.60, `VOX BUS` 0.95,
  `ORCH BUS` 1.10, `VERB RETURN` 0.70, `DELAY RETURN` 0.60.
- Four rhythm guitars panned ±0.8 / ±0.6; drum/room/overhead perspectives preserved;
  no double-imported alternatives.
- Master metric on the full song: **−9.93 LUFS integrated**, sample peak −2.43 dBFS,
  true-peak estimate −0.84 dBTP, crest 10.58 dB, LRA 2.87 LU, stereo correlation
  0.706, side/mid −7.63 dB, mono fold loss −0.69 dB, spectral centroid 1007 Hz,
  clipped samples 0. True peak is a 4× polyphase estimate, not a standards-compliant
  meter.

Frozen artifacts:

| Artifact | Path | SHA-256 |
|---|---|---|
| Final project | `work/benchmark/himmp/project/HIMMP-In-Solitude.rpp` | `e5458b3017b7e135284deb22adb32f99bff6408d7d22a61bf5aeccc89116f3ee` |
| Final master WAV | `work/benchmark/himmp/final/HIMMP-In-Solitude-Hermes-master.wav` | `0d8d50a447cc96a0b84b46d5355ce0fb4803a17135d63075286f5b7be2c9983c` |
| Freeze revision | 998 | after `reaper_save`; project hash above |

The master is 44.1 kHz, stereo, 300.52 s, 13,252,932 frames, 79,518,282 bytes.
The source files and reference files are unchanged.

---

## 5. Post-freeze objective comparison

Procedure: time-align each reference to the Hermes master using a 50 ms log-RMS
envelope cross-correlation (search ±12 s), trim to the common overlap, then apply a
single gain to the Hermes master to match that reference's integrated LUFS. No EQ,
compression, limiting or other correction is applied to Hermes. Band profile is a
27-band Welch spectrum (31.5–12.5 kHz). True peak is a per-block 4× polyphase
estimate; it is not a compliance meter. Reference masters are already about −10.4
LUFS; the gain applied to Hermes is about −0.51 dB. All numbers describe similarity,
not artistic rank.

Table: after gain-only match; band MAE/max are over 31.5–12.5 kHz; local MAE is over
5 s RMS windows; `lag` is the envelope alignment offset.

| Producer | Role | lag s | LUFS | Hermes TP dBTP | band MAE dB | band max dB | crest H/R dB | LRA H/R LU | corr H/R | side/mid H/R dB | local MAE dB |
|---|---|---:|---:|---:|---:|---:|---|---|---|---|---:|
| Research Team | team | 4.40 | −10.44 | −1.35 | 2.59 | 8.36 | 10.51 / 10.11 | 2.87 / 4.12 | 0.706 / 0.670 | −7.63 / −7.04 | 0.69 |
| Jens Bogren | producer | 1.35 | −10.44 | −1.35 | 2.04 | 8.66 | 10.56 / 11.16 | 2.99 / 2.79 | 0.706 / 0.552 | −7.63 / −5.36 | 0.29 |
| Mike Exeter | producer | 3.60 | −10.44 | −1.35 | 3.06 | 7.22 | 10.52 / 10.78 | 2.69 / 2.55 | 0.706 / 0.576 | −7.63 / −5.69 | 0.33 |
| Adam “Nolly” Getgood | producer | 0.95 | −10.45 | −1.39 | 1.54 | 3.74 | 10.52 / 12.20 | 2.65 / 3.24 | 0.709 / 0.564 | −7.69 / −5.55 | 0.06 |
| Josh Middleton | producer | 4.30 | −10.44 | −1.35 | 2.65 | 7.36 | 10.51 / 9.36 | 2.86 / 2.36 | 0.706 / 0.596 | −7.63 / −5.96 | 0.30 |
| Fredrik Nordström | producer | 3.80 | −10.44 | −1.35 | 2.52 | 5.84 | 10.50 / 10.64 | 2.63 / 2.03 | 0.706 / 0.345 | −7.63 / −3.12 | 0.71 |
| Buster Odeholm | producer | 3.95 | −10.44 | −1.35 | 2.90 | 10.19 | 10.50 / 8.49 | 2.65 / 2.48 | 0.706 / 0.623 | −7.63 / −6.34 | 0.12 |
| Dave Otero | producer | 3.70 | −10.44 | −1.35 | 2.34 | 4.63 | 10.50 / 9.99 | 2.60 / 2.85 | 0.706 / 0.602 | −7.63 / −6.05 | 0.05 |
| Andrew Scheps | producer | 3.80 | −10.45 | −1.39 | 2.46 | 4.32 | 10.46 / 12.80 | 2.51 / 3.66 | 0.711 / 0.452 | −7.71 / −4.21 | 0.23 |

Interpretation, measured only:

- Overall tonal distance is 1.54–3.06 dB MAE across the 8 producers; Nolly is
  closest (1.54), Exeter furthest (3.06). Team is 2.59.
- The largest single-band deviations are 3.74–10.19 dB; Buster has the largest
  (10.19 dB), so the aggregate MAE hides local differences.
- Hermes is consistently narrower than most references: side/mid −7.63 dB vs
  −3.12…−6.34 dB, stereo correlation 0.706 vs 0.345…0.623. This is a measured
  difference, not a defect claim.
- Crest 10.58 dB and short-term LRA 2.87 LU sit inside the producer range
  (8.49–12.80 dB; 2.03–4.12 LU). The master is not unusually squashed by these
  measures.
- Local 5 s RMS MAE is 0.05–0.71 dB, but envelope correlation is only 0.13–0.51;
  alignment lags vary 0.95–4.40 s because the reference masters are trimmed
  differently. The comparison is not sample-locked and is not suitable for
  time-local claims at edits/fades.

Artifact: `work/benchmark/himmp/analysis/objective-comparison.json` (includes
per-section 30 s MAE arrays and the reference hashes).

The model did not hear any master or stem. `not_heard` applies to the mixing model.
The analysis was numeric and file-based.

---

## 6. Role-stem comparison: not completed

The attempt to compare the four roles used normal track-stem rendering. REAPER
returned the 34 source stems plus master, but the receive-only bus tracks
(`DRUMS BUS`, `BASS BUS`, `GTR BUS`, `VOX BUS`, `ORCH BUS`) produced silent WAVs:
a track stem renders the track's own media/receives only in a way that does not
capture a pure aux return. The silent files and the comparison script were removed.

What this means:

- No Hermes-vs-producer role-stem table is reported. The category-stem set remains
  staged and licensed, but unanalysed.
- A correct role comparison needs a supported **bus/solo-stem render** that captures
  the aux return post-FX, with the same transaction/readback guarantees as other
  captures. MCP does not expose one. This is a concrete capability gap, not a
  reason to infer role balance from soloed close mics.
- Relative role balance must not be inferred from the provider's normalised category
  stems anyway; the original stems folder would be required for that.

---

## 7. Professional processing evidence

Full register: `work/benchmark/himmp/evidence/PROCESSING-EVIDENCE.md`.

- 25 sources inspected (video pages, interviews, input-list PDF, HiMMP pages).
- 96 tier-A/B verified observations, 10 explicit inferences, 12 open unknowns.
- Verified examples (producer, timestamp): Nolly chose Beta 91 + Subkick and used a
  ~40 Hz narrow boost/shelf on the sub (4:52–6:23); Bogren reamped only guitars 1–2
  and kept 3–4 tracked (11:55–12:44); Bogren’s master used an SSL-style compressor
  and A/D clipping (1:22–1:26); Scheps removed a bus compressor and used Pro-Q3 EQ
  matching (34:37–36:07); Exeter used a Townhouse bus compressor (1:13:38).
- Exact plugin lists, insert order, most parameter values, cab/mic details,
  automation curves, monitoring rooms and revisions remain **unknown**; the register
  separates what was said/shown from what was inferred from sound or description.
- Public materials disagree on whether a reference mix was supplied during the
  study; the report does not resolve that. No producer DAW session was found in the
  public archive, so exact-chain similarity is **unknown**, not reconstructed.

---

## 8. Tests, MCP surface, and documentation

Commands:

```
PYTHONPATH=src $TEMP/reaper-audio-library-python313/python.exe -m pytest -q tests
760 passed, 2 skipped, 3 warnings in 15.52s

focused: tests/test_wave_d_mcp_analyze.py tests/test_wave_c_analyze.py tests/test_wave_c_capture.py
54 passed in 5.43s
```

The two skips are live tests skipped by default. Wave C live evidence remains
**2 passed** on the disposable fixture; it is not repeated as a Wave D live gate on
this 41-track project.

MCP surface was reconciled from the live `tools/list`: **19 tools**, **5 resource
templates**. `README.md`, `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.ru.md`,
`docs/AI-INTERFACE.ru.md`, `docs/MCP-README-LLM.ru.md`, `docs/STATUS.ru.md`,
`docs/VALIDATION.ru.md` were updated from 12 to 19 where they were stale.
`docs/MCP-README-LLM.md` already said 19. The source of truth is the live
`tools/list`, not the prose.

Changed product files in this wave include `src/reaper_audio/mcp_server.py` and
`tests/test_wave_d_mcp_analyze.py` (MCP `tail_ms`/`channels`/`sample_rate`
pass-through and validation); the pre-existing dirty tree and ignore rules are
preserved. The final report is research documentation, not a release note.

---

## 9. Subagents and artifacts

Wave D used **3 subagents total, 3 completed, 0 active**; never more than two active
at once. The dataset worker staged the multitrack; the reference worker staged the
masters/stems; the evidence worker built the processing register. The coordinator
(Hermes) owned MCP registration, project construction, the mix, freeze, comparison,
tests and this report.

| Item | Path |
|---|---|
| Source manifest (43 files, 1.776 GB) | `work/benchmark/himmp/manifests/SOURCE-MANIFEST.json` |
| Input list / roles | `work/benchmark/himmp/manifests/INPUT-LIST.md` |
| Attribution / CC BY | `work/benchmark/himmp/ATTRIBUTION.md` |
| Reference manifest (45 files, 3.478 GB) | `work/benchmark/himmp/reference/REFERENCE-MANIFEST.json` |
| Processing evidence | `work/benchmark/himmp/evidence/PROCESSING-EVIDENCE.md` |
| Objective comparison JSON | `work/benchmark/himmp/analysis/objective-comparison.json` |
| Frozen project | `work/benchmark/himmp/project/HIMMP-In-Solitude.rpp` |
| Frozen master | `work/benchmark/himmp/final/HIMMP-In-Solitude-Hermes-master.wav` |

---

## 10. Remaining gaps

1. **Listening:** no human or audio-capable model has heard the Hermes master or the
   references; `not_heard` remains true. The user is the final judge of artistic fit.
2. **Role stems:** bus/solo-stem render is missing; the four-role comparison is
   unfinished and must not be inferred from silent stems.
3. **Reference analysis surface:** MCP has no external-WAV comparison tool; the table
   is offline analysis of staged files. Do not describe it as an MCP result.
4. **Final render path:** the frozen WAV came from the library's native render/capture
   path, not an MCP render tool. The project edits were MCP-bound; the render is not.
5. **Scale:** one song, one REAPER version (7.84), 41 tracks, 34 items; no general
   claim for arbitrary projects, tempo maps, automation or VST chains.
6. **Producer chain:** exact processing remains unknown; no session files were found.
7. **One-shot MCP resources:** analysis records are per-server-process; a separate
   process cannot release an analysis ID created by another process. Use a persistent
   session for analyze → mix-evidence → release workflows.

## 11. User listening form (pending)

Listen to, in this order:

1. `work/benchmark/himmp/final/HIMMP-In-Solitude-Hermes-master.wav`
2. one or more of the eight producer masters in `work/benchmark/himmp/reference/masters/`
   (the Research Team master is labelled separately)

Then answer briefly:

- Preference (Hermes / producer name / no preference):
- What is wrong or missing in the Hermes mix (low end, guitars, drums, vocals,
  width, dynamics, harshness, balance):
- Which reference is closest to the desired direction:
- Any single change to try next:

No “success” claim is made until that listening evidence exists.
