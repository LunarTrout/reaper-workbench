# Timing benchmark protocol

Status: protocol and harness, not a quality claim. Audio is never committed to
this repository; the manifest names public sources and their terms.

## What is being validated

The primary method is **audio quantisation by editing**: detect musical events,
move them to declared targets at the original playback rate, and handle the cut
edges with overlap and fades. Time-stretch/warp is a separate baseline, not the
method under test.

Evaluation is split into three independent tasks:

1. **Event detection** — where an attack, note, syllable or the relevant part
   of an attack begins in the source audio.
2. **Musical targeting** — which grid, tempo, groove or reference position the
   event should reach. Mechanical straight sixteenths are not assumed; targets
   and the permitted amount of correction are inputs.
3. **Edit quality** — whether attacks, durations, tails and pauses survive,
   without clicks, gaps, duplicated events or audible seams.

Two modes are required:

- **golden-events**: human-verified events and targets are supplied; this tests
  the edit engine without detection or target-selection errors.
- **full-pipeline**: audio in, final edit out; this tests analysis and target
  selection as well. A failure must name which of the three tasks failed.

## Data, by purpose

| Source | Role | Do not claim |
|---|---|---|
| GuitarSet | guitar event detection and metrical targeting; acoustic guitar, not high-gain riffs | that it represents distorted rhythm guitar |
| MoisesDB | real stems and musical context | that it is a corrected-timing reference until a concrete paired version exists |
| MedleyDB | real raw/stem material and context | that raw is an unedited timing reference; its raw audio is time-shifted to match stems |
| DALI, vocadito, MIR-1K, VocalSet | vocal events, note and syllable boundaries | that their annotation is the correct grid or a corrected-audio pair |
| ASAP | rhythm-target logic on performed MIDI plus score | audio edit quality |
| Groove MIDI | human performance vs quantised events; train/test by drummer | vocal or guitar audio |
| Sound On Sound Mix Rescue pairs | real before/after edits on full mixes or stems | exact millisecond truth unless the session/project is available |

Every manifest entry records source, performer/song where known, annotation,
split and usage terms. Audio is downloaded locally by the user and is not
redistributed by this repository.

### Real paired material found

- **Ollie Wright** — `guitarsuntimed` / `guitarstimed` (rhythm guitars and bass
  in context) and `lvunedited` / `lvedited` (lead vocal). Real performance,
  before/after human timing edits.
- **Tom McKenzie** — `Timing02_Unedited` / `Timing01_Edited` plus raw multitrack
  and the full Reaper remix project; the project exposes item positions,
  splits and source offsets. Best candidate for exact per-instrument truth.
- **We Fell From The Sky** — `DrumsUntimed` / `DrumsTimed`, more than 500 human
  drum edits; `BassOrig` for the bass source.
- **Wolf's Head & Vixen** — corrected/uncorrected full-mix section; drums were
  quantised with a relative shift on the second beat, other instruments edited
  to different degrees.
- **Lockdown Mixdown** — `original timing` / `corrected timing` full mixes.
- **April Blues** — raw vocal / vocal after pitch and timing fixes.
- **Cambridge-MT Chapter 6** — timing edits on bass/guitar and combined
  timing/tuning examples.
- **RysUp Align Bench** — audio-to-audio vocal alignment benchmark with real
  singer pairs and exact synthetic cases; useful for alignment metrics, not as
  the same performance before/after.

## Synthetic benchmark with exact truth

Take a source recording, declare its target events and grid, then move known
segments by known milliseconds. Record for every segment: source start/end,
target start/end, shift, source offset, overlap and fade. Primary cases use
editing with no rate or pitch change; separate cases use time-stretch/warp.

Vary attack density, shift sign and magnitude, pauses, long tails, legato,
trills and tremolo. For vocals, separately test consonant preservation and
vowel duration.

Synthetic recovery is necessary but not sufficient: it cannot prove behaviour
on a real mix.

## Real expert holdout

Use several manually aligned parts, including material from the owner's
project. Keep the decision record, not only the final audio: source attack,
target, cut point, extended region, overlap length and shape, preserved pauses
and tails. Where possible include a second person's edit; one author's version
is a reference, not a universal correct answer.

If detection is done on a clean signal and evaluation on a distorted one, keep
both and apply the same edit map to each; listeners judge the processed signal.

## Baselines

All baselines receive the same input, tempo, targets and processing chain:

- no-op;
- global phrase shift;
- uniform slicing;
- nearest-transient grid snap;
- time-stretch/warp;
- the proposed edit planner.

No parameter tuning on test songs. Splits are by whole song and performer, never
by random fragments of the same take.

## Metrics

- Events: precision, recall, F1 at a declared tolerance; false-attack rate.
- Targeting: mean and p90 timing error.
- Edit quality: missing/duplicated events, duration and pause change, signal
  gaps, boundary errors, seam artifacts.
- Blind A/B with randomised labels: evenness, naturalness, audibility of
  artifacts.
- Report separately by instrument and density; include failures. Do not reduce
  the result to grid agreement or waveform similarity alone.

## Splits and reporting

`dev` is used for thresholds and settings; `validation` is untouched until the
protocol is frozen; `holdout` is the real expert material. Report per source,
per instrument and per density. A claim that the method works for any instrument
requires independent performers and a real holdout.

## Files

- `manifest.json` — datasets, real pairs, licenses, roles and splits.
- `synthetic.py` — deterministic segment moves with exact truth.
- `baselines.py` — no-op, global shift, grid snap, time-stretch stub.
- `metrics.py` — event/timing/duration metrics.
- `run.py` — synthetic harness and JSON report.
- `tests/` — exact-truth and baseline-failure tests.
