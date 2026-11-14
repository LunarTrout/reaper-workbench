# Partial loudness: reconstruction gate and schema v2

Status: implemented in `src/reaper_audio/analysis/partial.py` and
`src/reaper_audio/analysis/bundle.py`, unit-tested with synthetic buffers only.
**Not validated on the real project**: no live capture or approved render was
used, so the gate's behaviour on this material is still the open question E3 in
`docs/research/round-5/SYNTHESIS-AND-DECISIONS.md`.

## Why the gate exists

`attribute_partial_loudness` subtracts each source's band power from the declared
node's band power and re-runs the loudness model on the residual. That arithmetic
is only meaningful if the supplied source renders really reconstruct the declared
node render. The previous version checked only sample rate, start time and frame
count, then always emitted `share_of_partial_loudness_pct`, `relative_audibility`
and a `5 %/45 %` flag in `mix_evidence`; a limited master or a partial stem set
produced confident-looking numbers for a mix that could not be reconstructed.

## What the gate checks

Decision D5-04 in the round-5 synthesis: no per-source values unless the gate
passes at the declared node, with median |Δ| ≤ 1 dB and p95 ≤ 3 dB per band
(`docs/GAP-ANALYSIS.md:457`). D5-05: after the gate, raw shares only, with
`n_sources`, node, stage and window; no audibility language and no fixed-percent
classification.

1. **Declaration.** The mix render must declare `node`, `stage`, `channel_view`;
   every source render must declare its own `node`, `stage`, `channel_view` and
   may declare its `window`. Missing or unusable declarations fail closed
   (`missing_provenance`).
2. **Consistency.** Every source stage and channel view must equal the declared
   mix stage and view (`stage_mismatch`, `channel_view_mismatch`). The stage
   label names the summing point, not each track's own processing: a node render
   taken after processing that is unique to that node (bus FX, a limiter) will
   pass the label check and then fail the residual check. Every source
   node must differ from the declared mix node and from every other source node
   (`node_mismatch`) — otherwise the same signal is counted twice or the mix
   node counts itself. A declared window must equal the measured window within
   half a sample (`window_mismatch`).
3. **Alignment.** Same sample rate, timeline start, frame count and channel
   count (`sample_rate_mismatch`, `timeline_mismatch`, `frame_count_mismatch`,
   `channel_count_mismatch`). These are reported as an unavailable payload, not
   raised, so one mis-declared stem cannot abort a whole capture analysis.
4. **Reconstruction.** The source renders are summed in the **time domain** and
   decomposed with the same filter bank the model consumes (29 third-octave
   bands for `iso532-2`, the 28-band Zwicker set for `iso532-1`). Per band and
   frame the dB error between the reconstruction and the declared render is
   measured; the gate passes when `median|Δ| ≤ 1 dB` and `p95|Δ| ≤ 3 dB`. Silent
   and above-Nyquist cells that both sides report as unmeasured are excluded.

Summing waveforms, rather than powers, is deliberate: correlated and
anti-correlated renders reconstruct exactly, and the gate then says so. A gate
built on power sums would reject them for a reason that is not a reconstruction
failure. The departure of the incoherent power sum from the node's band powers
is still measured (`gate.result.incoherent_power_sum`) and reported — because the
subtraction arithmetic assumes it is small — but it is **not** a second
threshold and it does not suppress a gate that passed.

## Output, schema v2

`partial_loudness.schema_version = 2`. Additive fields:

| Field | Meaning |
|---|---|
| `status` | `"available"` or `"unavailable"` |
| `reason` | first failing reason, or `null` when available |
| `reasons` | every failing reason, in priority order |
| `node`, `stage`, `channel_view`, `window` | the declared render being attributed |
| `n_sources` | number of sources admitted |
| `equal_share_baseline_pct` | `100/n`, descriptive only, never a gate |
| `share_denominator` | `"sum_of_source_partial_sones"` — shares are relative to the supplied sources, not the mix |
| `gate` | `method`, `method_version`, `status` (`pass`/`fail`/`not_evaluated`), `reasons`, `alignment`, `declaration`, `tolerances_db`, `result` |
| `gate.result` | `median_abs_db`, `p95_abs_db`, `max_abs_db`, `cells_evaluated`, `frames`, `bands`, `per_band_median_abs_db`, `incoherent_power_sum` |

On `unavailable`, `sources` is `{}` and no share, ratio, audibility label,
too-quiet/exposed flag or substitute estimate appears anywhere in the payload.

On `available`, per-source values are the model's raw arithmetic:
`solo_sones`, `partial_sones`, `partial_to_solo_ratio`,
`partial_to_solo_ratio_p10`, `sensitivity_sones_per_db`,
`share_of_partial_loudness_pct`, per-region and per-frame series. No threshold
or classification is applied.

Tolerances are keyword arguments `max_median_error_db` (default `1.0`) and
`max_p95_error_db` (default `3.0`); the values used are echoed in
`gate.tolerances_db`. They are the project's own proposal from
`docs/GAP-ANALYSIS.md:457`, not a literature constant and not a mix-quality
judgement.

## Migration

| Old | New | Action |
|---|---|---|
| `relative_audibility` (all levels) | `partial_to_solo_ratio` | rename; the old name claimed a percept the model cannot measure |
| `relative_audibility_p10` | `partial_to_solo_ratio_p10` | rename |
| shares always emitted | shares only when `gate.status == "pass"` | callers must branch on `status` |
| `ValidationError` on misaligned stems | `status="unavailable"`, `reason="timeline_mismatch"` etc. | callers that caught the exception must read the payload |
| `partial_loudness` without status | `status` + `gate` block | new required keys for consumers |
| `mix_evidence` issue `partial_audibility` (5 %/45 % flag) | removed; descriptive `mix_evidence.partial_loudness` block, only when available | consumers that keyed off the issue id must read the block instead |
| `mix_evidence.schema_version = 1` | `2` | bump for the removed issue type |

`analyze_bundle` passes `node`/`stage`/`channel_view`/`window` from each
`AudioBuffer.provenance` when present. Capture path provenance (as written by
`daw/session.py`) does not declare a node or channel view, so
`include_partial_loudness=True` now reports
`coverage.partial_loudness = "unavailable: missing_provenance"` until the
capture records those facts. That is the intended fail-closed behaviour: the
per-track renders and the limited master were never shown to reconstruct each
other.

## Tests

`tests/test_partial_loudness_gate.py` (synthetic, no REAPER project):

- exact linear reconstruction passes and the gate block is complete;
- median error just over 1 dB fails closed while p95 is inside its bound;
- p95 error just over 3 dB (two of 29 bands boosted, 6.9 % of cells) fails
  closed while the median is inside its bound;
- stage, node (mix node reused, duplicate source node), channel-view, window,
  missing-provenance, timeline and channel-count mismatches fail closed;
- the declared window must equal the measured section;
- a declared mix that contains a source never rendered fails closed;
- the `iso532-1` path gates on its own 28-band set;
- correlated/anti-correlated renders pass reconstruction, with the incoherent
  power-sum departure reported descriptively (13 dB in the fixture);
- a saturated (`tanh`) master-style sum fails closed;
- gate tolerances are echoed and overridable.

`tests/test_partial_loudness.py`, `tests/test_analysis_bundle.py` and
`tests/test_mix_evidence.py` were updated to the new semantics.

## Not claimed

- Not validated against a real capture, a real bus node or the open project.
- No claim that any share predicts audibility, masking or a mixing decision.
- No claim that the 1/3 dB rule is perceptual; it is the project's measurement
  validity tolerance.
