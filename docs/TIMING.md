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
4. Call `reaper_timing(action="plan", analysis_id=...)` to infer the rhythm
   and obtain a plan with its `rhythm_id`. Use `subdivisions=[4]` to explicitly
   select sixteenths across the item. An explicit grid uses divisions per
   quarter note. `grid_phase_s` is a seconds offset from the tempo map.
   Set `protected_ranges` to pin time windows. The summary reports the
   coverage of the detected fronts and the largest remaining errors.
   Use `detail="full"` or `inspect(plan_id=...)` for all blocks and seams.
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



## Review and refine a plan

A crossfade plan reports all detected candidates, the assigned subset,
and the number of assigned attacks that improved, stayed the same,
or moved farther from their targets. The counts use sample resolution
for comparison. An unassigned front may be a note attack, an ornament or
noise. Inspect the evidence before choosing a target. Match errors refer
to planned audio geometry. They do not verify the sound of a crossfade.

```text
reaper_timing(action="review", plan_id=..., review_kind="unassigned",
              start_s=12.0, end_s=13.0)
reaper_timing(action="review", plan_id=..., review_kind="residual")
reaper_timing(action="refine", plan_id=..., start_s=12.0, end_s=13.0,
              reference_events=[{"event_id":"evt_00012","qn":4.5}])
```

`review` also accepts `review_kind="all"` and pages with `review_offset` and
`review_limit`. `refine` creates a child plan before the original item is
replaced. It expands the requested interval to intersecting clips and keeps
their outer fades and every clip outside those boundaries. Any explicit
event targets must be reachable within the retained seams. A rejected
refinement leaves the parent plan available. The child report shows the old
and new error for each local target and how many surrounding clips were
kept. A plan with no timing movement cannot replace the item.

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

## Window review, verify and refine

`reaper_timing(action="review", plan_id=..., start_s=..., end_s=...)` switches
`review` from the compact batch view to one phrase.  Every attack row keeps
three separate times — `source_s` (seconds in the media file), `project_s`
(timeline) and `target_s` (musical goal) — plus `residual_s`, the nearest
seams, every block that reads it (`occurrences`, with `fade_attenuation`),
`blockers` and, for in-window attacks, a 41-point `envelope_db` at 2 ms steps.
Every seam row reports `cut_source_s`, both fade lengths and
`source_audio_left_s`; when the source runs out, `edge_source_exhausted` is
true and the remainder is reported as 0.0 instead of a negative number.
`detail="full"` adds a `signal` block and `conflicts`.

### Counting duplicate attacks

Do not count duplicates from the audio.  Two measurements disagree on dense
clipped material: an energy-front rule with a 20 % rise flags 99 % of attacks
(two fronts), and adding a 6 dB valley requirement only lowers it to 89-92 %,
while cross-checking against a librosa-based labelling gave 0.851 accuracy at
zero true positives.  A 30-75 ms attack rise simply produces several
threshold crossings.

The exact answer is geometric and comes free with the plan: `duplicate_geometry`
counts attacks whose source position is covered by two or more blocks.  It needs
no threshold and no audio.  `signal_echo` keeps the detector output for
reference and must not be read as "two attacks played".

### Local actions

`daw/timing_refine_actions.py` provides the per-event edits the agent needs:
`pin_attack`, `manual_attack`, `exclude_candidate`, `choose_rhythm_option`,
`protect_interval`, `recompute_seam`.  Each returns `(child_plan, changes)`,
never mutates its input, keeps every untouched block byte-identical, and
reports a `conflict` (insufficient source, neighbouring attack overlap, clip
under 4 ms) instead of moving a seam it cannot place.

### Checking the result

`daw/timing_verify.py` builds what REAPER will play (`render_plan`: sample
accurate block slices, linear fades, additive overlaps, silence outside) and
reports per attack whether the front is where the plan wants it, per seam
whether a front appears there, and a `conflict_report` covering source
overruns, attacks under a fade, gaps and blocks shorter than 4 ms.

## Worked example: the standard planner on Riff A

Measured on the real project, two clean takes, `subdivisions=[4]`,
`tolerance_s=0.03`, `profile="picked"`:

| take | blocks | median before | median after | worst residual >= 15 ms |
|---|---|---|---|---|
| riff 1 `{83EDFD23}` | 64 | 8.17 ms | 2.14 ms | none |
| riff 2 `{99C603E8}` | 53 | 9.82 ms | 2.29 ms | none |

Both plans applied with `readback.matched: true` and `duplicate_count: 0`, so
`analyze -> plan -> apply` is enough for material where every attack is within
the planner's reach. The per-event path is not needed there.

That run also shows the limit worth remembering: an individual attack with a
10-15 ms residual can stay exactly where it was (the front at DI `12.8249` s
played at `1.5583` s before and after), because the planner moves blocks, not
events. When one attack has to land on a specific sixteenth, use the per-event
path below.

## Worked example: one pick through manual_attack -> insert_seam -> apply

Confirmed case: the pick at DI `17.1955` s played at `68.5540` s, its target was
`68.5852` s (+31.2 ms). `manual_attack(source_s=17.1955, project_s=68.5540)`
creates the event (the detector never reported it), then `insert_seam` on that
id produces:

- the outgoing clip ends exactly where the pick would repeat (`68.5540`) with a
  fade that reaches zero there;
- the incoming clip starts one fade earlier (`68.5460`) reading source
  `17.1563` s, so the whole transient plays once, at `68.5852`;
- the outgoing seam's overlap equals both fades and the source reuse stays
  inside 60 ms, so `apply` accepts the geometry;
- the following seam is lengthened to absorb the source skip the move creates.

`tests/test_timing_refine_actions.py::test_manual_path_moves_one_pick_once_with_no_hole_and_keeps_neighbours`
reproduces this case numerically: the pick is audible once on target, the old
copy has zero gain, no hole appears on the timeline and the neighbouring attack
keeps its time.

### Validate a per-event refinement

For a missed front, call `refine` with `refine_action="manual"`.
Pass the DI onset as `source_s`, its current project time as `start_s`,
and its musical goal as `target_s`. Supply `attack_end_s` to check the
whole transient. Run `refine_action="seam"` on the new plan with the
returned `event_id` and the same `source_s`.

Run `apply(dry_run=true)` on the final child plan at the revision you
intend to apply. A refined front must pass in one clip outside its fades.
Pass the destination `track_guid` and working `item_guid` for both calls.
Review reports the remaining timing error. Use
`review(compare_plan_id=...)` to compare targets and errors between plans
from one source analysis, including events without targets.
