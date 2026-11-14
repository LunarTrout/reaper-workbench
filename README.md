# REAPER Workbench

**A Python library for context-aware REAPER control and audio analysis, with an optional MCP server for AI clients.**

Use it to inspect a live REAPER project, capture its tracks and master for analysis, compare audio, and make project edits through the documented ReaScript API. The library exposes project context and low-level capabilities; it does not prescribe a mixing or composition recipe.

[Русская версия](README.ru.md) · [Installation](docs/INSTALLATION.md) · [Architecture](docs/ARCHITECTURE.md) · [MCP guide for models](docs/MCP-README-LLM.md) · [Validation status](docs/VALIDATION.md)

> **Release status: pre-1.0.** The public API and MCP surface may change before the first stable release. Validation evidence and known limits are documented below; a passing unit suite does not imply complete ReaScript coverage or production readiness.

## Scope

The project has three layers:

| Layer | Responsibility |
|---|---|
| **REAPER bridge** | A resident Lua ReaScript executes requests inside REAPER through its scripting API and Actions. |
| **Python library** | Project context, typed API discovery, audio capture, measurements, comparisons, and revision-bound edits. |
| **MCP server** | An optional stdio interface that presents project-aware tools and resources to an MCP client. |

The bridge and Python client communicate through local request and response files. They do not use Frida, inject into REAPER, or inspect another process's private memory. The bridge currently relies on Windows `LOCALAPPDATA`; Windows is the supported development platform. Other operating systems have not been validated.

## Capabilities

### Project context and control

- Read project identity and revision, tempo map, tracks, master, routing, sends, items, takes, FX chains, and selected envelope or MIDI details.
- Identify project objects by GUID and bind calls to a project revision, rather than relying on track indices that may change.
- Search a bundled catalog of **732 documented ReaScript functions** (catalog generated from the official v7.85 reference), inspect signatures, and issue typed API calls. Runtime availability still depends on the installed REAPER version.
- Search and invoke REAPER Actions for operations that are available only through the Action List.
- Prepare supported edit batches, preview their intended targets and values, apply them against an expected revision, read back the result, and undo the latest transaction where REAPER permits it.

The catalog is a discovery surface, not a claim that every API function has been exercised or has a safe transaction wrapper. Generic API calls and Actions are low-level operations; they are not automatically covered by the library's preview/undo guarantees.

### Reference-corpus research tools

The optional `corpus` package compares declared reference material using
measured signal features, controlled-degradation benchmarks and explicit defect
gates. A separate experimental EQ cycle can test one measured candidate change
and reject it if the measurement moves the wrong way or a defect gate regresses.
These research tools are not part of the default capture path, do not prescribe
a mix workflow, and do not produce a universal quality verdict. Their dataset
scope, method, measured results and limits are documented in the
[roadmap](docs/ROADMAP.md), [policy](docs/POLICY.md), and
[measurement methodology](docs/MIX-QUALITY-METHODOLOGY.ru.md).

### Audio analysis

- Capture separate track render outputs and a separate master output through REAPER's offline render mechanism.
- Attach source identity, project revision, time range, render provenance, and available mixer context to the analysis report.
- Run the integrated measurement suite for the captured sources: level and loudness, dynamics, spectral balance, stereo and phase relationships, transients, temporal profiles, tempo context, and source/reference comparisons.
- Compare a project source with another file or a selected time window from a reference file.
- Produce structured mix evidence and edit evidence from measurements without encoding a fixed order of operations.

REAPER's Audio Accessor is a different signal tap: its samples are pre-FX and must not be described as processed mix stems. The current project-analysis path uses REAPER rendering. See [full-capture and analysis details](docs/FULL-CAPTURE.md) and the [validation status](docs/VALIDATION.md); never infer signal stage from a filename alone.

Analysis audio is stored outside the `.RPP` in a bounded job directory. It is leased for the analysis request and cleaned up on release or by the stale-job policy. The capture path may temporarily change render settings or track selection and restore them afterward; callers must use the returned revision rather than assuming the project's change counter is unchanged.

Full-capture master renders are silence-aware and fail-closed. The library checks the rendered master peak/RMS and classifies the result using the rendered source stems and captured REAPER state. A silent section, all sources muted, or an intentionally muted/zeroed master is accepted with `capture_status="valid_expected_silence"` and `master_validation.status="expected_silence"`. A silent master with audible sources and an audible signal path is unexpected: the library allows one bounded retry and preserves every failed attempt as archived evidence referenced by `timings.master_attempts`. If both attempts are unexpectedly silent, the capture is still archived for chronological evidence but the call returns the structured `capture_master_invalid` failure; `ok=true` is never returned with an unexplained invalid master. A successful retry returns `capture_status="valid_after_retry"` and keeps the failed attempt's hash/archive path. Production DSP analysis has a hard cap of 8 workers: windows up to 30 seconds may use up to 8, subject to source count and available memory; long or unknown windows default to 4. Counts of 12 and 16 are benchmark-only, not production settings.

#### What is measured, and what the model receives

One reaper_analyze request captures every non-master track and the master as separate outputs for the requested project-time range, then runs the available measurement suite over all of them. This is an offline REAPER render, not a real-time audio stream. The capture does not add tracks or items to the arrangement: WAV artifacts live outside the .RPP, are associated with a manifest, and are removed on release or lease expiry.

Every source is tied to its project revision, track GUID, time window, sample rate, capture method, and available track context. That context includes the track's current fader, pan, mute/solo state and available FX/routing information; the project tempo map is looked up at each point on the analysis timeline. A separate rendered master is analyzed in the same request. A track render and the master answer different questions, so neither is used as a substitute for the other.

The detailed analysis record contains measurements at several time scales. The roughly 100 ms feature tape belongs to that detailed record; it is not all copied into the initial model-facing panorama. The panorama shows a bounded, multi-resolution project-wide view and declares its time coverage and aggregation. For example, its cross-source RMS map has at most 24 timeline buckets; exact RMS, band-level and temporal-profile series remain in the capture record and can be retrieved with a revision-bound semantic query. The summaries do not replace or delete those source series.

The detailed report contains measurements at several time scales:

| Analysis | What it reports | How to interpret it |
|---|---|---|
| Level and dynamics | Sample peak, a 4× oversampled true-peak estimate, RMS, crest factor, integrated LUFS, loudness range when the section is long enough, clipped-sample counts, and DC offset. | These describe measured signal level and change over time. The true-peak value is an estimate, not a broadcast-compliance certificate. Short sections may not support a stable loudness-range estimate; coverage notes say when a value is missing or limited. |
| Frequency and tonal features | Average and time-varying levels in 28 one-third-octave-style bands centered from 25 Hz to 12.5 kHz (subject to the signal's Nyquist limit); spectral centroid, spread, flatness and roll-off; pitch-class/chroma distribution and an exploratory major/minor-template fit; and local fundamental-pitch and harmonicity estimates where the signal supports them. | These are signal features, not instrument labels or a guaranteed key transcription. Distorted, layered or polyphonic material can make pitch and tonal-center estimates ambiguous. |
| Time-local profile | A feature tape at roughly 100 ms intervals, including level, band energy, spectral change, attack/decay descriptors, pitch-related features, and stereo width, left/right energy balance and correlation where applicable. | Each cell is evidence about that section of audio, with timestamps on the project timeline. It is not a classification of the playing technique or musical role. |
| Events and transients | Short-window onset, level-spike and clipping candidates, plus spectral-flux transient candidates with low/mid/high-band strength and confidence. | These are candidate event locations derived from signal changes. They are not guaranteed beat labels, note boundaries or instructions to cut. |
| Tempo | The exact effective project tempo/time-signature context from REAPER's tempo map, attached along the timeline, plus a separate local BPM estimate derived from onset periodicity with confidence and plausible metrical alternatives. | The project map is the session's tempo authority. The audio estimate is independent evidence: dense attacks can represent beats or subdivisions, so a half/double-time ambiguity is reported rather than silently treated as certainty. |
| Stereo and phase | Stereo correlation, side/mid level, mono fold-down level change, and—when requested for selected track pairs—cross-spectral coherence/phase, correlation, relative delay and summed-power change. | These indicate relationships between signals. A low correlation or fold-down change alone does not prove an audible fault or say whether the width is intentional. Pairwise phase analysis is opt-in. |

Additional per-source values include modulation rate and depth, a pitch-jitter estimate, transient density, onset/spike/clipping event counts, and a coarse 500–4000 Hz energy ratio. The last value is explicitly a speech-band proxy; it is not a standards-based Speech Intelligibility Index or an intelligibility score. Every metric carries units and coverage/availability notes where the input duration, channel layout or signal quality limits the calculation.

The time tape also carries low/mid/high and ERB-like band energies, zero-crossing rate, harmonic-partial and band-decay estimates, and a derived “wall” descriptor. “Wall” is a library heuristic combining mid-band energy share and spectral flatness, not a standardized acoustic metric. Cross-source “masking” evidence is likewise a relative band-level dominance heuristic, not a psychoacoustic masking model.

#### Calculation path

The core pass is deterministic signal processing in Python, using NumPy and SciPy. Integrated LUFS and loudness-range measurements use pyloudnorm; the optional ISO 532 partial-loudness model uses phonometry. Librosa is an optional research dependency, not part of the default analysis path. Spectral summaries use Hann-windowed short-time Fourier transforms (4096-sample windows, 1024-sample hops by default). Power is calculated per channel and averaged, rather than summing left and right waveforms first, so an anti-phase stereo signal cannot disappear from the spectrum. Each time-local cell is calculated on an approximately 100 ms step and includes band levels and signal descriptors.

Two event detectors provide complementary evidence. A short-window envelope detector scans peak levels in 4 ms windows every 2 ms for onsets, spikes and clipped samples; it also records local RMS and event shape. A separate transient detector measures positive spectral-energy change in low, mid and high bands and ranks peaks against a robust local baseline. Local BPM candidates are then derived from the timing periodicity of onset events in rolling eight-second windows; this is not a general-purpose beat tracker and it does not replace REAPER's project tempo map.

The default bundle also compares each track's measurements numerically with the rendered master and reports cross-source band-level relationships over aligned time cells. These are deltas and relative level evidence, not an overall mix score or a verdict that one source is masking another. Explicit source-to-source all-pairs comparisons, phase-pair analysis, reference-file comparisons, and ISO-model partial-loudness attribution are available when requested; pairwise and partial-loudness work is not silently enabled for every project.

Optional partial-loudness attribution estimates how each aligned stem contributes to a modeled loudness of the mix, using an ISO 532 model and an assumed calibration reference. It is not an acoustic SPL measurement, does not prove masking, and does not reproduce nonlinear processing in a bus or master chain. Reference comparisons report metric deltas and, for multiple references, a median and median absolute deviation; they do not automatically declare a target mix.

The analysis is DSP-based: it measures levels, spectra, envelopes, periodicity and stereo relationships. It does not run an instrument-recognition model, identify a guitar or drum part by genre, judge whether a mix is good, or prescribe EQ, panning, compression or mastering targets. Any edit decision must combine these measurements with the actual audio, the project state and the user's stated goal. In dense or heavily distorted metal, transient and pitch candidates need particular care: fast attacks, cymbals, distortion and layered performances can confuse simple signal features.

The MCP result returns a per-source summary and URIs for the detailed report and captured audio. A source row contains its GUID, name, master-track flag, fader, pan, mute state, capture metadata, summary metrics and audio URI; the full report adds track context, time curves, event candidates, coverage, comparisons and provenance. In outline, the model sees a revisioned project summary and a list of records shaped like this:

~~~json
{
  "source_guid": "track-guid",
  "name": "Bass",
  "is_master": false,
  "volume": 0.8,
  "pan": 0.0,
  "mute": false,
  "capture": {"method": "native_reaper_offline_render", "time_window": {}},
  "metrics": {"peak_dbfs": {"value": -6.2, "unit": "dBFS"}},
  "audio_uri": "reaper://analyses/.../audio/track-guid"
}
~~~

The actual report also carries project tempo at each timeline cell, FX/routing context, and links to the complete curves and event list. For the model to literally listen to a captured source, its MCP host must fetch the audio resource and pass it through an audio-capable model path; exposing an audio URI alone does not guarantee that every MCP client will deliver sound to the model. That end-to-end audio handoff remains a validation item.

### LLM-facing interface

The MCP server currently exposes **29 tools** and **5 resource templates**. The interface covers project-context delivery (open, page, acknowledge, exact query, status, delta and refresh), a plan-bound candidate render (`reaper_render_candidate`), capability discovery, integrated analysis, supported edit batches, listener/spatial diagnostics, timeline context, and lower-level REAPER API functions or Actions. Candidate rendering uses REAPER's native master path and checks settings restoration and project state; if the check detects a mismatch, the WAV is deleted and the request fails closed. Live comparison against a normal REAPER render is still an open validation item; see [Validation status](docs/VALIDATION.md). The server does not publish one tool per ReaScript function.

Project context is a paged delivery of one view fixed to one capture and revision. `reaper_panorama_open` returns page 1 plus a manifest of every page id, payload hash and measured page size; `reaper_panorama_page` fetches the rest and `reaper_panorama_ack` verifies every page hash, reconstructs the exact `view_sha256` and mints the receipt plus context token. Pagination preserves that view without further truncation; the view itself is a bounded projection, so dense time series are summarized and linked to exact archived data. Page count adapts to the configured serialized-response budget; larger projects yield more pages. Mutating tools (`reaper_edit` apply/undo, `reaper_api_call`, `reaper_action` invoke, `reaper_save`) and project-target analysis/read tools (`reaper_analyze`, spatial/Flamingo project targets, `reaper_mix_evidence`, `reaper_edit_map`, `reaper_panorama_query`, and `reaper_capture_read`) reject a missing, stale or mismatched token. The bootstrap `reaper_capture_snapshot` is the only no-receipt capture operation and returns capture/archive metadata only, not the view. Exact high-frequency rows — relation edges, series cells, MIDI notes, envelope points — come from one generic semantic query (`reaper_panorama_query`) that returns a coherent fragment whole or reports `query_too_large` with its measured size. Deltas are bound to an exact base capture/fact root and their successor token is delivered in the same response; `refresh` is the normal recovery path after loss or staleness. The receipt guarantees delivery of these exact pages (view hash, revision, fact root), never that a model read or understood them.

The page parameters are named `*_bytes` for compatibility, but the current implementation measures the character count of an indented JSON rendering of each result; this is not a UTF-8 wire-byte count or the complete MCP transport envelope, and host-specific limits still need validation. Defaults are 30,000 characters target and 46,000 characters maximum per page. On the archived 41-track / 42-source reference capture the full model view is 46,977 compact bytes and is delivered in 5 pages (31,691 / 27,681 / 14,266 / 38,084 / 25,890 measured characters, max 38,084); the 101-source synthetic fixture (80,717-byte view) needs 12 pages, max 45,185. There is no page-count cap. A section larger than one page is split only at list-element boundaries with its head repeated; a single element that cannot fit raises with its measured size. The view keeps every track/master and all 24 scalar metric columns with units, window, stage and method provenance; exact item/take/audio rows, series cells, relation edges, MIDI notes and envelope points stay addressable by one semantic query. Inline scalar values are explicitly rounded, and `reaper_panorama_query(data=["analysis"], objects=[...], metrics=[...])` returns the unrounded stored values.

The model-facing contract is intentionally broader than preset musical workflows. Mixing, editing, arranging, MIDI authoring, and other tasks are expressed as goals assembled from project inspection, analysis, and REAPER operations; the interface does not impose genre rules or a fixed processing chain. See the [model guide](docs/MCP-README-LLM.md) for tool semantics and context handling, and [the decision record](docs/DECISION-single-response-panorama.md) for the delivery-history decisions.

### Working principles (not a mixing pipeline)

- Begin from the delivered whole-project view for the exact revision: fetch
  every page in the manifest and acknowledge them. Until `reaper_panorama_ack`
  returns `complete`, the map is partial and no analysis or edit is allowed.
  Exact queries fetch detail behind the map; they never replace it.
- Choose capabilities from the user's goal and the observed evidence. A full
  capability surface is not an instruction to run every analyzer or add FX.
- Keep project facts, measured signal properties, model hypotheses and the
  user's aesthetic judgment separate. Cite units, stage, window and provenance.
- Treat missing, stale, invalidated or incomparable evidence as a stated gap,
  not as silence or “no change”.
- After an edit, read the actual state back; if the change affects signal or
  arrangement, recapture before reusing old audio/measurement conclusions.
- Preserve alternatives and uncertainty; do not infer role or quality from a
  track name, one metric or a genre recipe.

Anti-patterns: treating a spill preview or a hand-reassembled set of query
answers as the full project view; mutating on a stale context token; applying a
delta to a different base; labeling a reduced projection “full”; hiding an
over-budget outcome; or inventing a better/worse verdict without listening and
evidence.

## Installation and connection

The project currently targets Windows, Python 3.13+, and a running REAPER installation. The Lua bridge is loaded by REAPER; Python or MCP clients run alongside it.

1. Install the Python package and copy the bridge Lua files into a dedicated folder under REAPER's resource-path `Scripts` directory.
2. Load `reaper_bridge.lua` from REAPER's Action List. Keep `lua_json.lua` beside it; the bridge runs as a deferred ReaScript.
3. Start the stdio MCP server from the same Python environment and configure your MCP client to launch `python -m reaper_audio.mcp_server`.
4. Start with `reaper_session(action="health")`, then read the project context and revision before making changes.

Full commands, client configuration, troubleshooting, and bridge lifecycle instructions are in [Installation](docs/INSTALLATION.md). The REAPER ReaScript API and Action List are documented by [Cockos](https://www.reaper.fm/sdk/reascript/reascript.php).

## Reliability model

Project-aware calls use stable identifiers and expected revisions. A supported edit is not considered successful merely because REAPER accepted a command: inspect the post-edit context and confirm the touched objects. If the project has changed since the plan was prepared, the plan is stale and must be rebuilt from fresh context.

This protection has defined limits. A generic ReaScript call or Action may have side effects beyond the typed transaction layer and may not be safely undoable by the library. Saving, overwriting, rendering, destructive editing, and project-file management require their own explicit checks. The safety model and current gates are documented in [Safety](docs/SAFETY.md) and [Validation status](docs/VALIDATION.md).

## Development

~~~powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,mcp]"
python -m pytest
~~~

Run the MCP server with:

~~~powershell
python -m reaper_audio.mcp_server
~~~

The server speaks MCP over stdio. It expects the REAPER bridge to be running in the instance that should be controlled. For API catalog maintenance, use `scripts/generate_api_catalog.py`; the generator reads the official Cockos reference and records its source version and content hash.

## Documentation

- [Installation and connection](docs/INSTALLATION.md)
- [Architecture and data flow](docs/ARCHITECTURE.md)
- [Full capture and analysis details](docs/FULL-CAPTURE.md)
- [Safety and project-state guarantees](docs/SAFETY.md)
- [MCP guide for language models](docs/MCP-README-LLM.md)
- [Validation status and release gates](docs/VALIDATION.md)
- [Russian documentation](README.ru.md)

## License

No license has been selected for this repository yet. Until a license file is added, all rights remain reserved; do not assume that the source is available for reuse.
