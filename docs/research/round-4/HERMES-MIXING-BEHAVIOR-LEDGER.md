# Hermes Mixing Behavior Ledger

## Purpose

Capture evidence about how an AI agent uses the REAPER library while working on a real mix. The ledger is source material for a later operator guide: which behaviors preserve context and produce verifiable edits, which behaviors create risk or waste effort, and what the interface or instructions should change.

This is a process ledger, not an automatic judgment of artistic quality. A measured change, a successful API response, or an LLM's own claim that something sounds better is not by itself evidence that the mix is better. The user remains the final listener and judge of artistic fit.

## Recording contract

Append one record per materially useful behavior or failure. Preserve earlier records; correct them with a linked follow-up instead of rewriting history. Keep observations separate from interpretations.

Each record should include:

- **ID and date/time**
- **Polarity:** `positive`, `negative`, or `ambiguous`
- **Evidence state:** `verified`, `partially verified`, `unverified`, or `superseded`
- **Project identity and revision:** exact disposable project, REAPER instance, revision/digest, and relevant time range
- **Task/context:** what the agent was asked to achieve and what project facts were available
- **Action trace:** MCP/API operation IDs or request files, relevant tool sequence, and readbacks
- **Observed result:** only what files, state readback, captures, tests, or listening establish
- **Interpretation:** why the behavior may help or hinder; explicitly label inference
- **Impact/risk:** what could have gone wrong or what was made easier to verify
- **Follow-up:** the next check or interface/documentation change that would prevent recurrence
- **Evidence links:** repository-relative paths, hashes where available, and external source/timestamps where relevant
- **Audio exposure:** `not_heard`, `metrics_only`, `audio_model_description`, or `direct_audio`. Do not label a run as heard merely because audio files or resource URIs existed on disk.

Use these labels narrowly:

- `positive` means the observed process advanced the user's goal while preserving project context, evidence, reversibility, or a clear readback. It does **not** mean the audio is artistically superior.
- `negative` means the observed process caused an incorrect/hidden edit, made a claim beyond available evidence, lost project context, or created avoidable operational risk. Identify whether the cause belongs to the agent, the library contract, the REAPER runtime, the project, or the source material; do not attribute a library defect to the model without evidence.
- `ambiguous` means the observation is real but its cause or consequence is not yet established.

## Initial records — HiMMP “In Solitude” experiment

### HB-001 — Blind first-pass boundary preserved

- **Polarity:** positive
- **Evidence state:** verified for the observed run setup
- **Project identity:** disposable HiMMP project at `work/benchmark/himmp/project/HIMMP-In-Solitude.rpp`
- **Task/context:** autonomous mix from the raw multitrack; professional mixes are reserved for post-freeze comparison.
- **Observed result:** the execution contract explicitly barred reference audio from the mixing model until the final project and full-song master were frozen. Local inventory showed no producer-reference audio staged in the benchmark directory when the first mix was made.
- **Interpretation:** this preserves an independent first pass and makes the later same-song comparison interpretable. It is a good experimental control, not evidence of mix quality.
- **Follow-up:** freeze the final project/master hashes and record the exact time at which reference material is introduced.
- **Evidence:** `docs/research/round-4/WAVE-D-AND-HIMMP-AB-EXECUTION-DISPATCH.md`; `work/benchmark/himmp/ATTRIBUTION.md`; `work/benchmark/himmp/manifests/INPUT-LIST.md`.

### HB-002 — Role buses and shared effects created in a staged edit

- **Polarity:** positive, limited to operation organization
- **Evidence state:** partially verified
- **Observed result:** `mix_stage1a.json` contains seven `track.create` operations: five role buses (`DRUMS BUS`, `BASS BUS`, `GTR BUS`, `ORCH BUS`, `VOX BUS`) and two effect returns. `mix_stage1b.json` contains 16 FX inserts, 40 sends, and 42 `track.set` operations. Those 42 set operations are 34 original tracks (linear fader value, pan, and `main_send=false`), seven added buses/returns (fader and pan), and the master (fader). For example, source-track values include `1. SubKick` at 0.56 with centered pan and direct-to-master disabled, and `30. Rhythm 1 Rig 1` at 0.22 with pan −0.8 and direct-to-master disabled. The API treats volume as a non-negative linear gain and pan as a value from −1 to +1; these are not 42 new tracks.
- **Observed result, routing detail:** the 40 sends comprise 34 source-to-role-bus sends (22 to DRUMS BUS, 1 to BASS BUS, 4 to GTR BUS, 3 to ORCH BUS, and 4 to VOX BUS) plus 6 sends from role buses to the returns (4 to VERB RETURN from VOX/GTR/DRUMS/ORCH BUS and 2 to DELAY RETURN from VOX/GTR BUS). The operation and APPLIED records establish those routes; they do not establish how audible or musically appropriate the return contribution is.
- **Observed result, later stages:** the stage payloads record 95 `fx.set_parameter` operations in stage 2; 4 more FX parameter changes and 25 track changes in stage 3; 6 track changes in stage 4; and one master-volume change in stage 5. Stages 1a through 4 have `APPLIED` records with `applied=true` and `dry_run=false` in their output files. Stage 5 has an operation JSON and an after-stage-5 capture, but no `stage5.out` file was present in the inspected workspace. The stage payloads total 236 operations; that count alone is not a quality measure.
- **Interpretation:** staged operations make the broad routing and processing choices inspectable and give later edits identifiable targets. They show that work went beyond adding seven buses, while still providing no evidence by themselves that the chosen levels, pans, routing, or processing sound musically appropriate.
- **Follow-up:** freeze the resulting routing/FX readback and link each material choice to the agent's stated evidence or intent. Compare producer-session decisions only when visible in source material; otherwise mark exact chain as unknown.
- **Evidence:** `work/benchmark/himmp/mix_stage1a.json`; `work/benchmark/himmp/mix_stage1b.json`; corresponding `stage1a.out` and `stage1b.out`.

### HB-003 — Same-window measurement used after a master-fader change

- **Polarity:** positive, limited to verification practice
- **Evidence state:** verified for the captured five-second segment
- **Project identity:** HiMMP project, revision associated with stage 4 then stage 5
- **Observed result:** the stage-5 master-fader change from 0.7 to 0.8 was followed by captures of the same 10–15 s window. In that segment, measured peak changed from −3.59 to −2.43 dBFS, estimated true peak from −2.00 to −0.845 dBTP, and LUFS from −11.18 to −10.02; clipping ratio remained zero.
- **Interpretation:** paired same-window captures make the numeric consequence of the edit visible. These values are local segment measurements, not integrated full-song loudness and not an artistic quality verdict.
- **Follow-up:** include full-song measurements and listening evidence in the frozen final report; preserve the exact analyzer method and capture hashes.
- **Evidence:** `work/benchmark/himmp/mix_stage5.json`; `work/benchmark/himmp/analysis/after_stage4_10_15.json`; `work/benchmark/himmp/analysis/after_stage5_10_15.json`.

### HB-004 — FX were initially inserted on the record-monitoring path

- **Polarity:** negative
- **Evidence state:** partially verified; corrected path must still be rechecked live
- **Cause category:** agent/API-call semantics and library/bridge contract
- **Observed result:** the initial `TrackFX_AddByName` call used `recFX=true`, placing the FX in the record-monitoring chain, so they were absent from the normal track FX list/mix path. The bridge was changed to use the ordinary FX chain (`false`) and its instantiation position was corrected. The old `fx_test.out` predates that fix.
- **Impact/risk:** a successful-looking insertion could leave the audible mix unchanged while the agent believes the requested processing is active.
- **Follow-up:** verify post-insert FX identity, chain type, order, enabled state, and a short before/after signal capture in the live disposable session. The MCP response should state which FX chain was modified.
- **Evidence:** `work/benchmark/himmp/fx_test.json`; `work/benchmark/himmp/fx_test.out`; `scripts/reaper_bridge.lua`; live readback after the bridge correction is still required.

### HB-005 — MCP helper derived the wrong checkout root

- **Polarity:** negative
- **Evidence state:** verified as a helper-path defect; actual installed import masked it
- **Cause category:** test harness / runtime setup
- **Observed result:** `work/benchmark/himmp/mcp_call.py` and `mcp_edit.py` used `Path(__file__).resolve().parents[2]` and constructed `PYTHONPATH` as `repo/work/src`, while the package lives at the checkout root under `src/`. The temporary Python environment could still import the package through its editable install, which does not prove the helper is portable or that its `PYTHONPATH` is correct.
- **Impact/risk:** a successful local call may be incorrectly attributed to the current checkout, or fail on a clean machine.
- **Follow-up:** fix root discovery and log `reaper_audio.mcp_server.__file__` through the exact helper process; verify it points to this checkout and repeat from a clean environment without relying on an editable install.
- **Evidence:** `work/benchmark/himmp/mcp_call.py`; `work/benchmark/himmp/mcp_edit.py`; direct import check recorded during Wave D.
- **Follow-up update (2026-11-12):** the current helper files now use `find_repo_root()` and set `PYTHONPATH` to the checkout's `src` directory. This fixes the originally observed `parents[2]` path construction; the record above is historical. Current clean-machine portability is still unverified because the helper also hard-codes a Python interpreter under `%TEMP%`.

### HB-006 — Independent first-pass project and master were frozen with hashes

- **Polarity:** positive, limited to artifact preservation
- **Evidence state:** verified for the frozen file hashes; model exposure history remains unverified
- **Observed result:** a durable manifest now records the original generated project snapshot, final project, full-song master, and raw-input manifest, with SHA-256, file sizes, time, format, and the available REAPER project identity. A REAPER process check showed the intended disposable project open and responsive, with no unsaved-change marker. All four recorded hashes matched on immediate verification.
- **Interpretation:** exact file identity and the first-pass boundary are now reproducible for post-hoc comparison. This does not prove the mix's sound quality or by itself prove what audio the model received.
- **Follow-up:** audit the Hermes transcript for reference exposure; re-hash all frozen artifacts immediately before A/B. Do not edit this frozen project; use a separate copy for any later reference-guided pass.
- **Evidence:** `work/benchmark/himmp/manifests/MIX-FREEZE-MANIFEST.json`; `work/benchmark/himmp/final/HIMMP-In-Solitude-Hermes-master.wav`; `work/benchmark/himmp/project/HIMMP-In-Solitude.rpp`.

### HB-007 — Master identity differs between baseline and later captures

- **Polarity:** ambiguous
- **Evidence state:** unverified cause
- **Observed result:** the baseline snapshot and later edit-session captures report different master GUIDs; later stages are internally consistent with the later GUID.
- **Evidence:** `analysis/baseline.json` reports GUID `{4A86632B-1CD4-42C6-843F-37F53F2F9C4A}` and 35 sources including master; `analysis/after_stage5_10_15.json` reports GUID `{787C218A-7769-4296-8830-BF9FF9B9CFB8}` and 42 sources including master.
- **Interpretation:** this could reflect a different REAPER project/session identity, a regenerated project, or an identity-reporting defect. The current evidence does not distinguish these causes.
- **Follow-up:** compare the actual saved project GUID, bridge-reported identity, open REAPER instance, and capture manifests; record whether this is an expected project recreation or a continuity fault. Never silently merge revisions across identities.
- **Evidence:** `work/benchmark/himmp/analysis/baseline.json`; subsequent `after_stage*.json`; `work/benchmark/himmp/project/HIMMP-In-Solitude.rpp`.

### HB-008 — The model-visible MCP path failed, then work continued through helper scripts

- **Polarity:** negative / process boundary failure
- **Evidence state:** initial failure verified; replacement registration and checkout import path verified; an actual model-visible call through the replacement entry is still pending
- **Cause category:** current MCP registration / request validation / bridge runtime
- **Observed result:** the Hermes session recorded two `mcp__reaper_mcp__reaper_analyze` calls that failed with `validation_error: not enough values to unpack (expected 4, got 2)`. A subsequent `reaper_session` call reported that its stdio subprocess was lost and that the operation outcome was uncertain. Later benchmark edits and captures exist in `mcp_call.py`, `mcp_edit.py`, and stage output files, but those artifacts do not by themselves prove that the model-visible registered server was healthy or loaded from this checkout. A separate `hermes mcp test reaper-workbench-benchmark` has since connected over stdio and discovered 19 tools; resolving `reaper_audio.mcp_server.__file__` under that entry's Python 3.13 interpreter and `PYTHONPATH` points to this checkout's `src/reaper_audio/mcp_server.py`.
- **Impact/risk:** a successful mix artifact can conceal a broken model-facing integration if the agent falls back to a custom terminal helper. That would undermine the user's requirement that the model reliably work through the library/MCP interface.
- **Follow-up:** once the delegated benchmark workers finish, refresh the active Hermes session's MCP prompt or start a clean session so the model sees `reaper-workbench-benchmark`, not only its cached `reaper-mcp` catalog. Fix the two-value/four-value validation mismatch in the entry that produced it; make one short model-visible MCP health/project/analyze/edit/readback/undo sequence against this disposable project; only then claim end-to-end MCP use. Treat the existing mix as a useful REAPER/API experiment until that gate passes.
- **Evidence:** Hermes session `local-68cbc44a7d95f974f8a0078cbc01378495f89cecdce2f96f71891886f9257906`, tool results at Unix timestamps `1794508383.7179143`, `1794508393.2538984`, and `1794508554.0593832`; `work/benchmark/himmp/mcp_call.py`; `work/benchmark/himmp/mcp_edit.py`; stage outputs.

### HB-009 — No successful model-facing listening step is evidenced in the first mix

- **Polarity:** negative / missing feedback loop
- **Evidence state:** verified from the Hermes session tool trace; helper-script outputs were not a listening event
- **Audio exposure:** `not_heard`
- **Observed result:** for the first-mix Hermes session, the only model-visible REAPER calls were two `reaper_analyze` attempts (both failed), two `reaper_session` calls, and one `reaper_project` call. There were no `reaper_flamingo_describe`, `reaper_flamingo_ask`, or `reaper_spatial_analyze` tool results. Audio capture files and numeric analysis output created outside that successful model-facing path do not prove the model heard the signal.
- **Impact/risk:** the agent could apply mix decisions from names, structure, and numeric summaries without the auditory evidence the user expects. The current result is an API-editing experiment, not a demonstrated listen-adjust-listen loop.
- **Follow-up:** after the dedicated MCP entry is visible to Hermes, deliver a managed audio segment to an audio-capable analysis tool and retain its audio hash, exact window, response, and project revision. Require the model to state whether it received direct audio, an audio-model description, metrics only, or nothing. Capture a post-edit listening/analysis pass as well.
- **Evidence:** Hermes session `local-68cbc44a7d95f974f8a0078cbc01378495f89cecdce2f96f71891886f9257906`; sanitized tool-result counts: `reaper_analyze=2`, `reaper_flamingo_describe=0`, `reaper_flamingo_ask=0`, `reaper_spatial_analyze=0`.

### HB-010 — Ambiguous REAPER instance selection was rejected safely

- **Polarity:** positive
- **Evidence state:** verified on the current-checkout MCP server
- **Observed result:** with two disposable REAPER instances open, `reaper_session(action="health")` without a `bridge_session_id` returned a typed validation error and listed both instance IDs/project paths. No edit was attempted. Supplying the explicit HiMMP `bridge_session_id` then selected exactly `HIMMP-In-Solitude.rpp`.
- **Interpretation:** refusing to guess is the right safety behavior. The model must learn to inspect instances and bind each subsequent call to the chosen project; the helper and README must not silently pick the first REAPER window.
- **Follow-up:** verify the same discovery → explicit selection sequence through Hermes's refreshed model-visible MCP tools. Keep the explicit ID in every edit and analysis call.
- **Evidence:** live output from `work/benchmark/himmp/mcp_call.py` against `reaper-workbench-benchmark`; selected IDs were recorded in the local run log.

### HB-011 — A legacy unmanifested audio job escaped automatic cleanup

- **Polarity:** negative / storage-retention gap
- **Evidence state:** verified; one stale orphan was safely removed
- **Cause category:** legacy artifact ownership / cleanup policy; exact origin of the missing manifest is unknown
- **Observed result:** the shared MCP analysis temp root contained 11 job folders (about 3.31 GB total). Ten current jobs had valid `reaper-audio-library` manifests, were 0.5–0.7 hours old, and each held a bounded lease. One 28.41-hour-old folder contained exactly 24 WAVs, 2,890,047,888 bytes total, all named with the matching `rali-<job-id>-` prefix; it had no subdirectories, no unexpected files, and no ownership manifest. The library's stale cleaner correctly skipped it because it could not prove ownership. The current Hermes session did not reference the old job. After verifying the resolved path under the application temp root, the exact file list, age, and names, the 24 temporary files and empty job directory were removed; C: free space rose from 461.54 to 463.75 GB.
- **Interpretation:** the current manifest-and-lease cleanup works for new captures, but a legacy/crash-era job without its manifest can remain indefinitely. Blindly deleting all manifestless content would be unsafe; silently ignoring it also leaves multi-gigabyte garbage.
- **Follow-up:** make every capture manifest durable before REAPER creates files and preserve it through interruption. Add a diagnostic for manifestless `rali-<job-id>-*.wav` directories that reports age/size and requests an explicit cleanup decision; do not auto-delete unverified paths. Confirm that leased current jobs disappear after release or TTL.
- **Evidence:** `%TEMP%/reaper-audio-library/mcp-analysis`; old job `job-4351254a8408450eaf6471c8f9bb260c`; source naming logic in `src/reaper_audio/analysis/artifacts.py`; current job manifests and `MIX-FREEZE-MANIFEST.json`.

### HB-012 — Self-review misreported return routing and temporarily mixed up stage boundaries

- **Polarity:** negative, limited to self-review accuracy
- **Evidence state:** verified against operation payloads and APPLIED responses
- **Observed result:** Hermes's self-review (`HERMES-SELF-REVIEW.md`, §6, item 6) says all 40 sends went to role buses and suggests VERB RETURN and DELAY RETURN may be unfed. `mix_stage1b.json` and `stage1b.out` instead show six return sends: four to VERB RETURN from VOX/GTR/DRUMS/ORCH BUS and two to DELAY RETURN from VOX/GTR BUS. The review's §2.4 also lists stage-4 bus values (DRUMS 0.32→0.5, BASS 1.3→1.45, GTR 1.6→1.6, VOX 1.0→0.95, ORCH 0.9→1.1, MASTER 0.65→0.7) while discussing stage 3, then later moves those values to stage 4. The actual `stage3.out` has individual source trims and different bus changes; `stage4.out` contains the listed values.
- **Interpretation:** the self-review is useful for surfacing uncertainty, but its summaries need to be checked against operation-level evidence. Its explicit stage correction is visible, while the return-routing statement remains uncorrected in the report.
- **Follow-up:** keep Hermes's report intact and append an independent fact-check; use the stage payloads/APPLIED records as source of truth for route and stage claims. Do not infer a dormant FX return when a send exists; assess actual signal contribution separately.
- **Evidence:** `work/benchmark/himmp/evidence/HERMES-SELF-REVIEW.md` §§2.4, 6; `work/benchmark/himmp/mix_stage1b.json`; `work/benchmark/himmp/stage1b.out`; `work/benchmark/himmp/stage3.out`; `work/benchmark/himmp/stage4.out`.

### HB-013 — Self-review provenance timestamp and session identity are ambiguous

- **Polarity:** ambiguous
- **Evidence state:** mismatch verified; cause unknown
- **Observed result:** the report header attributes itself to mixing session `local-68cbc44a7d95f974f8a0078cbc01378495f89cecdce2f96f71891886f9257906` and gives review time `2026-11-12T23:15:00+03:00`. The actual reviewing Hermes session was `local-62bf18173e65268c0965d4dfb6619b4e1bc2801897fcaf85d65a9e7b6943f610`; its completed response and file write were logged around 22:32, and the report file modification time was 22:31:45+03 while the local clock was 22:34+03.
- **Interpretation:** the report's session ID names the mix being reviewed, not the session that authored the review, and its timestamp is later than the observed write time. A timezone/configuration discrepancy is possible but not established.
- **Follow-up:** record both `mix_session_id` and `review_session_id`, and derive the review timestamp from the host when writing the artifact.
- **Evidence:** `work/benchmark/himmp/evidence/HERMES-SELF-REVIEW.md`; Hermes session export for the two IDs; `hermes logs` completion entry; filesystem modification time.

## Hermes self-review required after the mix is frozen

Ask Hermes to review each record and the complete action/revision/capture history. Require it to distinguish:

1. what it knew before each decision from what became known afterward;
2. which decision was supported by audio, project state, measurements, producer documentation, or only an assumption;
3. when it actually heard audio versus received only metrics or text;
4. which edits it verified, reverted, or left uncertain;
5. what positive habits should be retained, what failure patterns should be avoided, and what MCP/API or README changes would make those behaviors easier;
6. what it cannot infer about professional plugin chains or artistic intent from the available evidence.

Hermes's self-review is another attributed interpretation, never a replacement for the raw trace or a user listening judgment. Link each claim in that review back to ledger records and evidence files.
Store the review, with its session ID and timestamp, in `work/benchmark/himmp/evidence/HERMES-SELF-REVIEW.md`; preserve the ledger as the source record and append any later corrections instead of replacing earlier evidence.

### HB-014 — Пользовательская оценка первого микса: баланс и FX

- **Polarity:** negative for current Hermes mix; positive versus dry sum
- **Evidence state:** direct user listening feedback; subjective and not a technical diagnosis
- **Observed result:** after listening to the dry sum, the Hermes mix, and a commissioned HiMMP producer reference, the user reported: the human producer's mix is very high quality; the Hermes mix is currently poor, though better than the dry version. The user specifically described the instrument balance as poor and said effects seem insufficient, without naming a particular effect or processing defect.
- **Interpretation:** this is evidence about the perceived result, not evidence that any particular processor or effect type is missing. Do not translate it into a fixed instruction to add effects. The next mix iteration needs an audio-grounded diagnosis of which sources are masked or unbalanced and what spatial/dynamic treatment, if any, would address the heard differences.
- **Follow-up:** preserve this judgement as the acceptance target for the next iteration. Require a successful model-visible listen/analysis path and read-back of current source levels, pan, routing and FX before proposing edits. Compare the same musical passage from the dry sum, Hermes render and producer reference at matched playback level; record the agent's stated evidence and the user's later judgement separately. Do not claim improvement until the user listens and confirms it.
- **Evidence:** user listening feedback in the active conversation on 2026-11-12; `work/benchmark/himmp/initial/HIMMP-In-Solitude-unprocessed-sum.wav`; `work/benchmark/himmp/final/HIMMP-In-Solitude-Hermes-master.wav`; `work/benchmark/himmp/reference/masters/2. Jens Bogren Mix_HiMMP -10.4 LUFS.wav`.
### HB-015 — Пользователь закрепил ответственность библиотеки за полный контекст

- **Polarity:** negative against the current integration; confirmed product requirement
- **Evidence state:** direct user requirement and current-session tool trace
- **Observed result:** the user clarified that the library itself must expose the complete usable REAPER capability and keep the current project state in context. The user objected to treating terminal scripts as an acceptable way for the model to work with REAPER. The current Hermes trace confirms that recent capture/edit-related operations were issued through terminal helpers; earlier model-visible MCP analysis calls failed validation and the stdio process was lost. A separate `hermes mcp test` finding 19 tools proves only that the entry can connect in that test, not that the active model session can use it.
- **Interpretation:** this is a library/MCP product and integration gap, not a request for the model to rely on memory or infer hidden state. Helper scripts do not demonstrate the model-facing capability.
- **Follow-up:** use the release gate in `docs/research/round-4/MCP-CONTEXT-RELEASE-GATE.md`. REAPER operations, context retrieval, audio delivery, edits, readback and undo must be demonstrated through the active model-visible MCP. Scripts may independently calculate or verify artifacts after the MCP action; they must not substitute for the interface.
- **Evidence:** user's clarification in the active conversation on 2026-11-12; Hermes session `local-68cbc44a7d95f974f8a0078cbc01378495f89cecdce2f96f71891886f9257906`; tool trace includes terminal-invoked `ReaperBridge.capture_render` at message 49065 and terminal-based benchmark scripts; previous MCP failures are recorded in HB-008 and HB-009.

### HB-016 — Пользователь слышит узкий, тусклый и «подводный» результат

- **Polarity:** negative for current Hermes mix
- **Evidence state:** direct user listening feedback; subjective description, not a diagnosis
- **Observed result:** while comparing the Hermes mix with the human producer's version, the user described the Hermes mix as sounding “underwater”, lacking panorama, dull/weak in its frequency balance, and generally lifeless; the user also reiterated that the professional mix sounds excellent. These descriptions identify the audible gap to investigate, but do not establish that a particular EQ curve, panning recipe, or effect is the cause.
- **Independent measurements already available:** the frozen Hermes master has side/mid energy of −7.63 dB and stereo correlation 0.706; the eight producer masters span −3.12 to −6.34 dB side/mid and 0.345 to 0.623 correlation. The full-song report also records 1.54–3.06 dB aggregate band MAE against the eight producers. These support a narrower stereo image and measurable tonal differences, but do not explain the user's “underwater” impression or identify which tracks caused it. The current band comparison is mono-downmixed and envelope-aligned rather than sample-locked; no valid role-bus comparison exists.
- **Interpretation:** the mix did not have an audio-grounded diagnostic loop when it was created: the mixing model had not heard the master or stems. It worked from project text and measurements, so it could not validate masking, source prominence, transients, depth, width, or whether chosen effects actually improved the audible result. This is a concrete process/context gap and a plausible contributor, not proof that it is the only cause.
- **Follow-up:** compare the same musical passage from the dry sum, Hermes master and producer master at matched loudness; preserve stereo L/R and M/S views; relate audible differences to source/bus levels, pan, routing, FX and section-local measurements. Add a supported post-FX bus/solo capture before making role-level claims. Treat normalized producer role stems as within-role references only, not relative-balance ground truth. Require the model to state what audio it actually received and cite the time range and signals before proposing changes. Record the user's listening judgement separately from numeric differences; do not convert the terms “underwater”, “dull” or “not enough effects” into a fixed correction recipe.
- **Evidence:** direct user listening feedback in the active conversation on 2026-11-13; `work/benchmark/himmp/final/HIMMP-In-Solitude-Hermes-master.wav`; `work/benchmark/himmp/reference/masters/`; `docs/research/round-4/WAVE-D-BENCHMARK-REPORT.md` §§1, 5–6; `work/benchmark/himmp/evidence/PROCESSING-EVIDENCE.md`.

### HB-017 — User rejects the 13 November candidate as MCP-mix evidence

- **Polarity:** negative for end-to-end MCP acceptance
- **Evidence state:** direct user clarification; overrides the candidate report's earlier acceptance wording until a model-visible trace proves otherwise
- **Observed result:** the user states that the candidate mix was produced by another Hermes using separate/out-of-band tools, without the full project context through the MCP surface. The WAV and snapshot may remain useful diagnostic artifacts, but the user does not accept this mix as proof that the library-enabled model can perform the work.
- **Interpretation:** a successful REAPER render, snapshot, or helper-script call does not prove the active model used the current MCP interface or made decisions from its context. The report's phrase “through MCP operations” conflated the underlying control path with the model-visible tool path.
- **Follow-up:** do not count this candidate toward MCP mixing acceptance. Require a trace from the active model session showing current MCP discovery, one linked snapshot/revision, audio delivery that the model actually consumed, and revision-bound edits/readback. Use user-provided reference masters only; do not expose hidden producer stems as mixing inputs.
- **Evidence:** user's correction in the active conversation on 2026-11-13; `work/benchmark/himmp/candidates/final-20261113-064144/FINAL-REPORT.md` (superseded by its “Acceptance status correction” section).
