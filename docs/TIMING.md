# Timing audio in REAPER

[Русский](TIMING.ru.md) · [Handbook](HANDBOOK.md)

`reaper_timing` analyzes a file-backed take before track FX. The default
`method="crossfade"` creates real REAPER media items at 1x with individual
crossfades. Use a clean DI take for guitars. A separate amplifier on the
track can remain active for playback.

## MCP workflow

1. Read the handbook and project context. Resolve the actual track and item
   GUIDs. For A/B, copy the unedited items to a separate track and edit the copy.
2. If the project has a dedicated click item, call
   `reaper_timing(action="clock", item_guid=..., start_s=0, end_s=8)`.
   It reports measured click fronts and their deviations from the quarter-note
   grid. Inspect the tempo and beat phase before adopting the click as a target.
3. Call `reaper_timing(action="analyze", item_guid=..., profile="picked")`.
   Analyze the whole item before replacing it. The response includes the
   observed attack front, peak, novelty, prominence, energy change and
   confidence. A detected front is a candidate.
4. Call `reaper_timing(action="plan", analysis_id=..., subdivisions=[4],
   tolerance_s=0.046, strength=1.0)` when the intended rhythm uses
   sixteenth notes. Choose the musical subdivision from the performance,
   including a different division for triplets. The argument is subdivisions
   per quarter note. `grid_phase_s` defaults to zero and is an explicit offset
   from the tempo map. Set `protected_ranges` to pin time windows that must
   stay in place. Inspect the chosen `anchors`, `seams`, and `diagnostics`.
5. Use `preview` for a dry A/B of the item window. Use
   `reaper_timing(action="apply", plan_id=..., expected_revision=...,
   reason=..., dry_run=true)` to check the plan against REAPER and the file.
   Then apply with `dry_run=false`. Require `readback.matched`, save the
   project and audition both versions through the same signal chain. The
   apply result gives the transaction ID and revision for undo.

## Infer a mixed rhythm before editing

Use this flow when one subdivision does not describe the entire performance:

```text
reaper_timing(action="analyze", item_guid=..., profile="picked")
reaper_timing(action="rhythm", analysis_id=..., tolerance_s=0.046)
reaper_timing(action="inspect", rhythm_id=..., rhythm_offset=8, rhythm_limit=8)
reaper_timing(action="plan", analysis_id=..., rhythm_id=..., tolerance_s=0.046,
              rhythm_choices={"bar_2_beat_3": "option_2", "bar_2_beat_4": "keep"})
```

The analysis ID and rhythm ID belong to the same MCP server session.
`rhythm` reads the current tempo and meter. It ranks ordered, one-to-one
assignments by beat, tests regular and tuplet subdivisions, and returns up to
three distinct assignments per beat. Its slots use divisions per meter beat.
For example, in 4/4, a division of 4 is a sixteenth-note grid. The optional
`subdivisions` list controls what families to consider for this action.

- Each option contains measured event IDs, candidate project targets in
  seconds and quarter notes, the distance to the next assigned attack
  within the beat, unmatched fronts, and observations behind the choice.
- The observations report the gap to the next candidate separately from
  measured release, when detectable. Vocal and sustained profiles also show
  early and late periodic pitch cues where available. These readings can be
  missing or unreliable in overlapping voices or heavily reverberant audio.
- `context_analysis_ids` can supply separately analyzed takes such as drums.
  A nearby contextual attack is supporting evidence. Relative grid patterns
  elsewhere in the item are also listed for comparison.
- `supported` means the observed fronts favor one assignment under this
  scoring model. The raw costs and margins are ranking scores. An
  `ambiguous` or `unresolved` beat defaults to `keep`, preserving its
  project timing. Choose a displayed option explicitly to resolve it.
- For a score, manually chosen grid goals, or targeted overrides, crossfade
  `plan` accepts `reference_events=[{"event_id":"evt_00012","qn":4.5}]`
  or a `target_s` instead of `qn`. Supply explicit references as a separate
  planning mode. Assignments must preserve attack order and stay within the
  tolerance. Use `protected_ranges` to pin disputed windows in this mode.

The selected map feeds explicit event targets into the native crossfade
planner. The result includes selected options, achieved shifts, coverage
checks, and checks that kept intervals stay in place. Validate the plan
before applying it. Timing inference can preserve an undecided passage rather
than force the grid, and note-level transcription of overlapping voices is
outside this tool's current scope.



## Plan decisions

- Attack evidence combines spectral novelty with low, mid, and high band
  rises. Nearby strong evidence estimates local timing drift to choose the
  most plausible grid cell. The correction is measured against the actual
  grid, not the drift estimate.
- Several nearby hits can move rigidly together. A new cut has a cost. If
  a feasible seam cannot be found, the affected groups are combined and
  the retained timing error is reported.
- A seam must avoid the neighboring fronts and other credible attacks.
  Candidate locations and fade widths are scored by the energy change in
  the blend, waveform correlation, excess high frequency change and local
  level. Both overlapping clips keep reading their own offset of the source.
- The plan keeps the existing item extent, preserves the outside fade
  lengths, and covers the project timeline continuously with paired fades.
  A small portion of source material can be skipped between seam handles
  when closing a timing gap. The apply receipt enumerates these source
  intervals. Source file bounds and pinned material are checked before apply.
- Before and after error figures are for the selected attacks and the
  resulting edit geometry. They do not quantify unselected note timing or
  establish whether the seam sounds transparent.

The `legacy` method retains the older analysis/plan surface for callers
that explicitly need it. Profiles set attack detection spacing and protection
for different sources. Validate vocals and sustained sources with their own
material and rhythm. This path edits a file-backed 1x item, and does not
reconstruct stretch markers, layered takes, or per-take processing. Prefer
track FX when retaining the same processing across an A/B.
