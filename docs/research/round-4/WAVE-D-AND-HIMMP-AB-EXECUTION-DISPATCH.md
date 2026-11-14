# Wave D and same-song metal-mix benchmark — execution dispatch

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


**Owner:** Hermes coordinator  
**Model:** `deepseek/deepseek-v4.1-flash-1017`, ultra reasoning  
**Workspace:** current `reaper-audio-library` checkout only  
**Maximum delegation:** two active subagents. Preserve all current dirty files and ignore rules.

## Objective

Continue toward a reliable, context-rich REAPER audio library and prove that an LLM can use it on a real multitrack project. Wave C established a strong capture/digest baseline, but it did not assess mix quality. Do not redefine completion as a green unit suite.

Start the same-song mix experiment as soon as the current library can be reached through a verified MCP connection and the source multitrack is staged. Do not wait for every Wave D context feature to be finished before attempting the first mix. Report this early mix as an experiment, not a release certification.

The first mix must be autonomous: the mixing model may inspect the source tracks and all available project evidence, but must not receive or listen to any professional reference audio until its final mix and project state have been frozen and hashed. The reference set is for post-hoc comparison. A later, separate reference-guided pass may use a professional mix selected after the user has heard the first result.

No fixed metal recipe, genre-specific constants, prescribed FX chain, or single hard-coded task pipeline. The model chooses operations from project context, its own listening/analysis, user intent, and measured results. Keep the operation history and evidence linked so the model can tell what each change affected and whether it was kept or undone.

## Baseline evidence to verify

Read `docs/research/round-3/WAVE-C-LIVE-REPORT.md` and the Wave C sections of `DECISION-LEDGER.md`. Recheck the current checkout before relying on that report.

- Wave C reports **725 passed, 2 skipped**; the skipped checks are live gates by default.
- A dedicated REAPER 7.84 test instance passed two live gates on `work/live-test-waveb/bastard_edit_ogg.RPP`.
- The live digest reported full coverage for tracks, items, takes, FX, sends, tempo, master, automation, envelopes, project settings, and FX state.
- One 1.25-second captured master matched an independent native REAPER render sample-for-sample.
- This only proves capture/revision/render behavior on one small fixture. It does not prove human listening, mix quality, full-scale behavior, all automation types, or another REAPER version.

The current source implements `ReaperSession.analyze(tail_ms, channels, sample_rate)`, while the MCP `reaper_analyze` tool does not expose or pass those options. Add those optional fields to the MCP schema and pass them through with validation and tests. Preserve the exact project/file/measured-window distinction and the truthful `restore_report` behavior.

The README, validation page, MCP guide, tests, and source disagree on the MCP tool count/status. Reconcile them from the live `tools/list` response and actual source. In particular, do not leave a claim of 12 tools if the server advertises 19.

The configured Hermes entry named `reaper-mcp` points to a Python environment outside this checkout; it is not proven to load this source tree. Do not overwrite or remove an existing entry. Create a separate temporary entry for the experiment and prove its loaded `reaper_audio.mcp_server.__file__` resolves to this checkout before claiming the model used this library. Use a stable Python 3.13 environment for the MCP process; do not leave a lasting configuration that points into a temporary directory.

## Work split

Use no more than two active workers:

1. **Dataset worker:** inspect the official HiMMP input-list PDF and inventory the source materials; stage only the coherent raw inputs needed for `In Solitude` in the ignored benchmark directory. Do not download or expose pro reference audio to the mixing model yet. Produce a file manifest with exact names, sizes, SHA-256 hashes, and source IDs/URLs. Do not edit product code.
2. **Context/MCP worker:** implement only the missing MCP pass-through for `tail_ms`, `channels`, and `sample_rate`, its schema validation/tests, and a source-linked context-ledger/view proposal or implementation that follows the current R3-05 research. Do not edit the REAPER Lua digest or dataset files.

Hermes owns merge review, current-source MCP registration/verification, isolated REAPER setup, the autonomous mix run, full and live tests, reference comparison after mix freeze, documentation reconciliation, and the final evidence report. Keep the scope boundaries; if a dependency requires touching another owner's files, coordinate first and report it.

## Dataset and licensing

Use the official University of Huddersfield HiMMP archive. The same `In Solitude` recording has eight commissioned producer mixes; the archive also publishes a research-team mix, loudness-matched masters, category stems, interviews, and mixing-session videos. Its FAQ states that the dataset is CC BY and permits reuse with attribution. Record attribution locally and keep downloaded audio out of Git/public product files.

- Official share: `https://huddersfield.app.box.com/s/8gren2ma4kesvf5vwip2axzz1v8sawur`
- Licensing/citation FAQ: `https://himmp.net/faq.html`
- Hi-res audio and stems overview: `https://himmp.net/audio.html`
- Producer sessions: `https://himmp.net/videos.html`
- Methodology: `https://himmp.net/approach.html`
- Main source folder `1) Project Song 'In Solitude'` (`169784180412`); its `1) Mix Files` folder is `169785262043`.
- Core `2) HiMMP Multitrack` folder is `169785155878` (44 listed files, paged 20/20/4); read its input-list PDF before deciding which optional alternatives to import. Do not import the bundled `43. HiMMP Rough Mix.wav` into the autonomous model's project or expose it to the model before mix freeze.
- Eight pro final comparisons are in `2) Producer Mixes of 'In Solitude'` (`171312398857`) → `3) Loudness-Matched Masters for Comparison` (`214901424770`), 9 masters total including the research-team mix. Use the eight producer files as the post-hoc comparison set; keep the team mix separately labeled if included.
- Category comparison stems are in `4) Loudness-Matched Stems for Comparison` (`214097340560`) with nine producers per role: drums (`235842947307`), bass (`235843750823`), guitars (`235843033966`), vocals (`235844092282`). They are role-normalized to differing LUFS targets (drums/bass −26.5, guitars −21.5, vocals −22); compare within a role across producers. Do not use these normalized role stems to infer relative drum/bass/guitar/vocal balance in a producer's master.

Stage files only under ignored `work/benchmark/himmp/`. Confirm there is ample free disk before a bulk download. Preserve the original downloaded inputs as the benchmark source; all derived temporary stems/renders must be manifest-owned and removed. Keep only the source set, one frozen Hermes final project/mix, the required reference comparison files, and concise manifests/reports. Never accumulate intermediate mix versions.

The public source discovered so far contains WAV mixes/stems and videos; no DAW session for a producer has been identified yet. Verify the archive inventory. Do not claim exact plugin names, insert order, or parameter values from sound alone. For processing-structure comparison:

- Use category stems to measure how each producer's drums, bass, guitars, and vocals differ in tone, dynamics, width, and time-local behavior.
- Use source/session videos and interviews for FX/route decisions only when the decision is visible/audible and time-stamped. Record source, timestamp, and confidence. Separate observed facts from inference.
- Compare Hermes's actual REAPER structure (tracks, buses, pans, FX identity/order/on-off, parameters where readable, sends, automation) to the decisions supported by video/interview evidence. Where producer project files or settings are unavailable, mark exact-chain similarity **unknown** rather than guessing.
- Treat the eight pro mixes as a distribution of valid artistic choices, not one ground-truth target. Public materials disagree on whether a reference mix was supplied during the study; preserve that uncertainty in the report.

## Autonomous REAPER/MCP mix acceptance

1. Download and verify the source set and input list. Create a disposable REAPER project under `work/benchmark/himmp/`, with no source-file edits. Include only the source inputs justified by the input list; keep alternative DIs/takes clearly identified and do not accidentally double them.
2. Launch a dedicated REAPER instance on that project. Verify project path, track/item counts, audio device/state, source hashes, and available MCP project identity before editing. Never attach to, save, or close a user's production project.
3. Connect the Hermes model to the **current checkout's** MCP server through the dedicated experiment entry. Verify `tools/list`, call health/project-context, and record the server module path. No direct private-memory access, Frida, or out-of-band audio injection.
4. Give the model the user-level goal: mix the provided full metal song professionally, preserving its performance/arrangement and making evidence-based creative choices. Do not give it producer reference audio or a prescribed sequence/FX chain. It must use project discovery, context, audio capture/listening/analysis, and REAPER operations as needed; every edit must be revision-bound/verified and reversible where supported.
5. Preserve a before fingerprint and source hashes. Make edits only in the disposable project through the MCP/API path. After each material edit, read back the affected state and use the library's analysis/listening tools to judge the result; retain or undo based on evidence. If a needed action is unavailable, stop at the capability boundary, implement it in the library/API, test it, then continue. Do not silently bypass missing MCP tools with ad-hoc UI automation or a hand-written mix recipe.
6. Freeze and hash one final project and one full-song final master before revealing reference audio to the mixing model. If the model cannot complete the mix through available tools, preserve the partial result only as diagnostic evidence, clean intermediates, and report the exact missing capability; do not claim an A/B success.
7. After freeze, stage the eight producer loudness-matched master WAVs and category stems. Compare the Hermes master against all eight pro masters with time alignment and gain-only loudness matching. Report objective distances and their units (integrated LUFS, true peak, tonal/band profile, dynamics/crest, stereo/mono behavior, temporal sections); these describe similarity and do not rank artistic quality.
8. Compare the four role stems by role, and inspect available session videos/interviews for processing decisions. Produce a table of verified setup choices (what the source proves), inferred choices (with confidence), and unknown exact chains. Do not reconstruct unavailable producer sessions or claim that a plugin was used unless evidence supports it.
9. Provide the user a listenable Hermes final and clearly labeled reference mix(es), plus a simple preference/notes form. The user is the final judge of artistic fit. Keep the original dataset inputs and one final result; delete temporary analysis captures and all intermediate mixes.

## Context-ledger / model-interface acceptance

Follow `docs/research/round-3/R3-05-CONTEXT-GRAPH-AND-MCP.md` and its adjacent briefs. Context must be relational and traceable: project/instance/revision → track/item/take/FX/route/time-map → audio asset and time window → measurement/event/listener note → claim/hypothesis → proposed operation → state delta → verification/undo. Every claim must carry the exact scope, source, stage, channel view, time basis/window, method, revision, uncertainty, and evidence references; stale, partial, conflicting, or unavailable inputs must be visible and must block unsupported conclusions.

Implement or complete an append-only project ledger with strict schema/versioning, stable typed IDs, dependency sets, asset ownership/retention, safe project separation, and derived views. Keep bulky audio and dense time series in managed artifacts; store hashes/URIs and compact summaries in the ledger. Provide a compact Tier-0 project brief and paged/filterable Tier-1/deep views so the model can see important context without receiving hundreds of unrelated fields. Preserve discovery paths to all lower-level REAPER capabilities; no one-tool bottleneck and no 732-tool flood.

Expose the analysis options supported by Python in `reaper_analyze`; ensure the MCP response carries linked sources for every ordinary track **and one processed master**, context values such as track pan/fader/FX and BPM/tempo-map status, exact project/file/measured windows, metrics, event candidates, and managed audio resources. If an audio-capable model does not actually receive audio, state `not_heard`; a URI or text analysis is not proof of listening.

Add adversarial MCP-level tests for: absent/ambiguous capability discovery; missing/stale revisions; a false `coverage=true` without payload; two open REAPER instances; multiple projects; hidden tracks/master; tempo-map automation; missing audio delivery; long-project pagination; contradictory analysis evidence; edit/readback/undo; old resource expiration; and huge tool discovery without flooding. Run headless protocol checks plus at least one call from the actual Hermes agent to the current MCP server.

## Required final evidence

Report separately:

- subagent count started/completed/active;
- exact current server source path and MCP `tools/list` count;
- changed source/docs files and clean-diff check;
- focused and full test commands/counts;
- live disposable REAPER gates and hashes before/after;
- A/B benchmark input manifest, license attribution, reference names, loudness/alignment procedure;
- whether the autonomous model actually received and listened to the stem/master audio;
- final mix path/hash, project path/hash, state/FX/routing summary;
- objective similarity table and explicit limits; user listening remains pending until the user hears it;
- verified vs inferred vs unknown professional processing-chain evidence;
- gaps that remain, especially supported editing domains, scale, and REAPER versions.

Do not call the library production-ready or the mix professionally successful based only on tests, metrics, or similarity. Keep the task active until the current MCP path, a real end-to-end mix attempt, and the user's listening evidence are handled.
