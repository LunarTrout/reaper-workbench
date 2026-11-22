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
5. Use `preview` for a dry A/B of the item window and a synthesized click from the plan’s tempo and meter map. Use
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

## Inspect a missed front

Use `review(review_kind="unassigned", start_s=..., end_s=...)` for candidates
the detector found but the plan left without a target. Use `window` with a
`plan_id` for anchor placements, local seams, fades and short envelopes
around known attacks. If you suspect a front **between** detected events,
ask for a continuous clean source window. It works even with zero candidates.

```text
reaper_timing(action="waveform", plan_id=..., start_s=68.48, end_s=68.72,
              waveform_bins=240, subdivisions=[1, 4])
```

Use `item_guid` to read a take before analysis, `analysis_id` for detected
candidates or `plan_id` to include assignments and planned occurrences.
Supply project-time bounds and zoom as needed. Each request covers up to
four seconds and returns up to 1024 bins. Each bin contains signed minimum,
maximum and RMS in linear sample units across channels, without normalization.
Bin edges are file sample indices. Divide by `sample_rate` to get source
seconds. Map a source time to the original item with
`item_position_s + source_s - source_offset_s`. The grid is an overlay,
including the plan phase if applicable. It does not assign targets.

Read nearby beats before choosing a front. Zoom in to distinguish the initial
rise from a later peak, and consider the neighboring notes, click and other
takes when relevant. A missing detector mark does not establish silence.
Two rises may belong to one pick. If the musical target is unclear, compare
alternatives in context or defer that event while continuing elsewhere.

## Work iteratively and verify what changed

Start wherever the available evidence is weakest. You can compare two draft
plans from the same analysis with `review(compare_plan_id=...)`. Use the
same DI events as the comparison basis, including events without a target.
Inspect the passage before increasing the correction or cutting more clips.

For a confirmed missed front, create an event with
`refine_action="manual"`: `source_s` is its onset in the file, `start_s`
is the observed timeline position in the **current input draft**, and
`target_s` is the musical goal. Pass `attack_end_s` to protect the entire
transient. Then use `refine_action="seam"` with the returned event ID, that
same DI coordinate and the child `plan_id`. For known events, inspect their
targets and actual clip occurrences first. If a seam fails, inspect both
neighbors and the available source handles. Broaden the local window when
that gives a clean transition. A safe remaining error can be a better result
than a split or doubled attack.

| Avoid | What to check |
| --- | --- |
| Treating a lower median as a finished riff | Compare identical events and goals. Look at unassigned, unchanged and worsened events, especially audible trouble spots. |
| Selecting the nearest event ID or loudest peak | Confirm the same file and source coordinates on the clean DI. Check the onset and the whole transient against the rhythmic context. |
| Cutting on every detector peak or forcing a single grid on every passage | Compare plausible subdivisions and match the musical pattern. Use as many cuts as the material needs. |
| Checking only that a clip covers the target time | Calculate where the **same source front** actually plays, using position and source offset. Inspect all its occurrences and fades. |
| Extending an edge through a second copy of the attack | Check that the intended transient passes intact once, the old occurrence is suppressed, and no gap or damaged neighboring attack appears. Use 1x clips with real source handles and paired fades. |
| Applying a parent plan, the original take or a second montage on top | Check the working item and track GUIDs, current revision and final child plan. Dry-run that exact plan before applying. |
| Repeating an apply that may already have written clips | Read the actual project and transaction result. Verify the rollback or recover from a known copy before retrying. |
| Reporting sound quality from `readback.matched` | Read-back confirms clip geometry. Trace the front again in the written clips. Use preview for dry A/B and listen with the click and intended track FX when available. State which checks were actually performed. |

Repeat local checks where evidence points to a problem, carrying forward the
latest verified draft and preserving good seams. Each pass should resolve a
specific uncertainty. Avoid rebuilding an accepted passage just to chase a
number. Work autonomously across clear cases within the user's scope. When
the user wants to judge successive versions, keep a comparable version and
use their feedback to guide the next pass. If the agent cannot audition
audio, distinguish the verified geometry from the listening check. Musical
intent or an unavailable input can leave a specific decision open.

For a closer check, `verify` simulates the dry plan and reports signal fronts,
source overruns, gaps and fade conflicts. Its signal estimates and simulated
fades are evidence for auditioning, especially on dense or clipped audio.
`window(detail="full")` adds signal diagnostics to the local clip report.

## Try moving a whole phrase first

When a line or riff is consistently early or late, first test a rigid move
of the whole phrase. Its notes keep their relative timing. Use individual
attack edits when the deviations vary within the phrase. Confirm musical
counterparts before choosing a shift. A lower median can hide a word that the
same move makes worse.

On a working copy, isolate the phrase at quiet boundaries and leave usable
space beside it. Use the existing item editing tools for this preparation.
`phrase_shift` takes the GUID of a single isolated 1x item and an explicit
`shift_s` in seconds. Positive values move the phrase later. There is no
per-attack quantization tolerance on this move. It preserves length, source
offset, rate and existing fades. It returns the old and new item windows and
blocks a move that would overlap or pass another item on the same track.
Resolve any boundary conflict before applying. The move can leave a pause.

```text
reaper_timing(action="phrase_shift", track_guid=..., item_guid=...,
              shift_s=0.312, reason="Move the confirmed phrase later")
reaper_timing(action="phrase_apply", plan_id=..., expected_revision=...)
```

For a double, pass the two analysis IDs, window and confirmed `event_pairs`
to the preview. It shows the predicted residual and whether it worsens for
each pair. Read those results before applying. After apply, check the same
pairs with `pairs` and inspect both neighbors. Audition the phrase with its
surroundings when available. A shift that helps some words and displaces others
calls for a smaller phrase or targeted edits.

## Verify timing across related takes

Choose a reference performance for the phrase and check its relationship to
the arrangement. Determine which events should actually land together.
A consonant, vowel onset, pick attack or release may be the useful landmark,
depending on the material. Compare the same landmark on both tracks using
short waveform windows, neighboring musical events and the click when relevant.
Keep intentional offsets and ornaments explicit. An ambiguous beat or an
unassigned candidate is unresolved evidence. It does not prove good timing or
a missing note.

Analyze both takes before editing. Use `pairs` with a short project-time window
to see unpaired candidates, then supply confirmed counterparts in performance
order. Keep the same analyses and pairs for the read-back after applying.

```text
reaper_timing(action="pairs", reference_analysis_id=..., analysis_id=...,
              start_s=130.5, end_s=132.0,
              event_pairs=[{"reference_event_id":"evt_00012","event_id":"evt_00017"}])
```

The result reports original and live deltas (second take minus reference),
all clip occurrences in the window, and unpaired candidates for each take.
`placement_verified` describes a unique, online, unmuted 1x occurrence outside
a fade on each track. It confirms placement of the supplied points. The tool
does not establish that two detections represent the same musical event or
measure their audible alignment through FX. Unpaired counts include detector
candidates, which may be ornaments or noise. Page through the whole window
before using the summary. Leave room at the bounds for both occurrences.

Use confirmed reference positions as explicit targets for local `refine`
or `plan`. If a front was missed, inspect its source and register it before
editing. Include the last child `plan_id` for manually registered events on
the edited take, or `compare_plan_id` for manual events on the reference.
Pairs are still read from the live clips. If a target or a possible pairing is disputed, leave it open or compare
alternatives. Work across the requested passages, review different phrases
and transitions after application, and state what remains unverified. Two
good pairs, a lower grid median, a good bus LUFS reading and a matched
transaction each answer different questions. Check the doubles together
and against the arrangement when listening is available.

## Examples from the guitar edits

- **Riff A:** the front at DI `12.8249` played at `1.5583` before and after,
  14.9 ms from its proposed goal. Other attacks improved and the user accepted
  the phrase after listening and inspecting the seams. The measured residual
  describes this edit. It supplies no universal tolerance.
- **Riff B:** the missed pick at DI `17.1955` played at `68.5540` in the
  base and belonged at `68.5852`. An early draft hit the goal while leaving
  another audible copy. The verified revision put the pick on the goal and
  ended the outgoing clip where its earlier occurrence was faded to zero.
  The input seam overlapped by 8 ms in this case. A longer output fade was a
  separate listening check. Both numbers describe that edit.
