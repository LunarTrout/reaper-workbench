# Timing analysis and grid planning

`reaper_timing` measures a file-backed audio take against the project grid and
returns a 1x clip plan. `apply` replaces the source item with those clips in one
revision-bound transaction; the analysis actions themselves do not edit.

## Actions

```text
reaper_timing(action="analyze", project_id=..., track_guid=..., item_guid=...,
              start_s=None, end_s=None, profile="auto",
              sensitivity=1.8, min_gap_s=0.03)
```

Resolves the active file take, detects attack candidates, refines each to the
first rise of its attack in the sample domain, and maps source time to project
time using the item position and `start_offset_s`. Returns `analysis_id`,
`source_path`, `project_window`, `events[]` with `source_s`, `project_s`,
`sample`, `kind`, `confidence`, and `event_count`. Only 1x takes are accepted;
MIDI items and changed playrates are refused with a typed error.

```text
reaper_timing(action="plan", analysis_id=...,
              subdivisions=[4,3,6,8] | None,
              tolerance_s=0.08, strength=0.85,
              group_gap_s=0.25, group_shift_s=0.012,
              protected_ranges=[{"start_s":27.0,"end_s":27.2}],
              reference_events=[{"target_s":...} | {"qn":...}])
```

Matches events monotonically to the tempo grid (or to explicit reference
targets), groups events whose required shifts agree, and returns `plan_id`,
`blocks[]`, `issues[]`, `alternatives[]`, and the median error before/after.
A block carries source start/end, target start/end, `shift_s`, source offset,
rate 1.0 and fade lengths. No event is assigned twice and no attack is cut by a
block boundary. `adjacent_shift_jump` is reported when neighbouring blocks
differ by 30 ms or more, including a jump across a protected pause.

`reference_events` is an ordered list of project targets. The same measured
events therefore produce different plans for different rhythmic references;
when the caller supplies two candidates that are close, both belong in
`alternatives` and one anchor can be pinned to recalculate only its phrase.

```text
reaper_timing(action="inspect", analysis_id=... | plan_id=...)
```

Returns the stored analysis or plan. `plan` already carries project positions
for every block (`project_target_start_s`, `project_target_end_s`).

```text
reaper_timing(action="apply", project_id=..., plan_id=..., reason=...,
              expected_revision=..., dry_run=False)
reaper_timing(action="undo", project_id=..., plan_id=..., reason=...)
```

`apply` checks the item is a file-backed 1x take without stretch markers, the
plan covers the item's source window, every clip stays inside the source file
and no protected range is cut or moved. It then sends exactly one
`item.timing_apply` operation, reads every created clip back (position, source
offset, length, playrate, fades, duplicates, leftovers) and returns the
post-apply revision plus the undo contract. `undo` uses that revision-bound
receipt and verifies the original item geometry is restored. The bridge build
is `b27`; the transaction is atomic and does not create a second edit engine.

## Evaluation

`benchmarks/timing/` holds the protocol, real audio-pair manifest, exact-truth
synthetic generator, baselines and reports. The real-pair report labels
cross-correlation estimates as measured shifts, never as human ground truth;
real audio is downloaded locally and is not committed.

## Coordinates and units

- `source_s` — seconds in the take's WAV, independent of the item position.
- `project_s` — seconds on the project timeline.
- `qn` — quarter notes on the REAPER tempo map; the grid is built through
  `TempoMap`, not from a constant BPM.
- Times in plans and issues are seconds unless a field says `_ms`.

## Current limits

The plan is the reviewed artifact. `apply` is implemented and unit-tested
against a fake bridge, but native A/B on a live REAPER project has not yet been
run in this repository. A 1x edit cannot preserve a take FX chain or take
envelopes; the operation reports that limitation instead of copying them.
The detector is signal-driven; the `profile` selects event naming, not a
musical genre or a fixed threshold. Protected ranges and anchors are caller
inputs. The real holdout currently has nine downloaded pairs; the remaining
manifest entries need their local audio before they can be scored.
