# Wave D follow-up — full-project tail boundary

This is a focused coding task for a separate Hermes run. Work in the current
checkout and preserve all existing dirty changes. Do not create subagents.

## Verified defect

`tests/test_wave_d_mcp_analyze.py` passes 11 tests, and the current live MCP path
passes when an explicit section is supplied. That does not cover a full-project
capture with a render tail.

In `src/reaper_audio/daw/session.py`, the no-section path currently sets
`project_end_s = rendered_duration`. With `tail_ms > 0`, this includes REAPER's
requested render tail in the measured project interval. The helper also sets
`tail_included_in_measurement=true`. This contradicts the `reaper_analyze`
contract and the explicit-section behaviour: the tail extends the audio file,
not the measured musical interval.

## Implement now

1. Trace the actual REAPER render setup and identify the strongest available
   source for the boundary: requested render-tail setting, resulting sample
   count/sample rate, and any authoritative project-end value. Compute the
   project end from verified values at sample-frame precision. Do not infer it
   from item names, musical structure, or hard-coded project length. If the
   rendered result cannot distinguish project audio from tail reliably, fail
   closed for this measurement and report the boundary as unknown.
2. Keep `project_end_s` / `measured_end_s` at the project boundary; allow only
   `file_end_s` / the WAV duration to extend by the tail. Report
   `tail_included_in_measurement=false` for every valid capture.
3. Add regression tests for: no explicit section + zero tail; no explicit
   section + nonzero tail; explicit section + nonzero tail; render shorter than
   the requested tail or otherwise ambiguous boundary; invalid values. Assert
   sample-frame rounding tolerance and that the file can retain the tail while
   the analysis window cannot.
4. Run focused tests (`test_wave_d_mcp_analyze.py`, `test_daw_session.py`), then
   the complete suite. Include exact interpreter, command, counts and output.
5. Run the corrected no-section/nonzero-tail path against a disposable short
   REAPER project via the actual MCP stdio endpoint and read the returned audio
   resource. Prove the actual file duration and measured end separately,
   confirm master and all tracks are present, call `reaper_release`, and verify
   cleanup and unchanged project content. Never open or save a user project.

## Constraints

- Do not change the dataset worker's `work/benchmark/himmp/` files.
- Do not change the contract by simply removing the tail option or skipping the
  full-project case.
- Do not claim full tail correctness from explicit-section tests alone.
- Do not spend this task expanding the ledger proposal or README.
- Report verified versus unverified items explicitly and include source/test
  evidence. Stop when the fix and its regression/live proof are complete.
