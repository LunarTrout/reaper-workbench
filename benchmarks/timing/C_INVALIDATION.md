# Old MY ALIGNED results are invalid for algorithm evaluation

Status: **invalidated**, 2026-11-21.

The files named `*_MY_ALIGNED.wav` and every C-vs-B number reported before this
notice are not valid evidence for the timing engine.

Why:

- the old C was built by measuring local shifts between the source A and the
  human-corrected B, then moving A by those measured shifts;
- B was therefore an input to C. The comparison C-vs-B was leakage, not an
  independent evaluation;
- most old C files were a flat WAV imported as one item. That does not show
  that the Workbench applied the edit inside REAPER; it shows a renderer.

Do not reuse those shifts, those C files, or those numbers for acceptance.
They may remain on disk only as historical, invalid material.

The replacement C must be built from A plus an independent metrical reference
(project grid, tempo map, metronome or an independent performance), then
applied through the Workbench transaction and read back as real REAPER items
with cuts, source offsets, lengths, fades and 1x playback. B, when present, is
a separate human reference and is only compared after C is fixed.
