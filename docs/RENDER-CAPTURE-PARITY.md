# REAPER native render capture: parity check

This report compares the library's bridge capture with REAPER's own render
command. It records render-path behavior only; it does not assess mix quality or
audio-model accuracy. Tests used an isolated multitrack project in REAPER 7.84.
No project file or audio is included in this repository.

## Compared paths

Both paths rendered the same open project revision with identical range, sample
rate, channel count, normalization, dither, tail, and add-to-project settings:

| Path | Mechanism |
| --- | --- |
| Native render | REAPER render action `Main_OnCommandEx(42230, 0, project)` |
| Library capture | `project.capture_render(master_only=True)` through the resident bridge |

The render bounds flag is `1` for the whole project and `2` for the time
selection. A prior short-range check with flag `1` rendered the whole project;
that result was discarded. Section tests must set the time selection or use
explicit time bounds and verify the returned frame count.

## Short section

The 30–40 s render used 44.1 kHz stereo PCM_24:

| Measurement | Native | Capture |
| --- | ---: | ---: |
| Frames | 441,000 | 441,000 |
| Peak | −4.364 dBFS | −4.365 dBFS |
| RMS | −18.642 dBFS | −18.643 dBFS |
| Bytes | 2,646,690 | 2,646,690 |

Sample-aligned subtraction gave residual RMS −47.32 dBFS and depth −28.67 dB.
Two native renders through the same path differed by −46.82 dBFS RMS, so the
capture difference was at the same scale as render-to-render variation in this
project.

## Full-project render

Both outputs contained 13,626,335 frames (308.987 s) and 81,758,700 bytes.
Measured RMS was −18.108 dBFS for both. Peaks differed by 0.337 dB; the
sample-aligned residual was −38.31 dBFS RMS, or −20.20 dB relative depth.
This project contained nondeterministic analog-modelled effects and sampled
instruments, so byte- or sample-exact equality is not a valid general
expectation for a processed mix.

## Deterministic control

With one dry track and all other tracks muted, repeated renders were sample
identical: the difference array was exactly zero. The capture matched REAPER's
own render for that deterministic signal path. The WAV file hashes still
differed because REAPER writes Broadcast Wave metadata (including time reference
and UMID); compare decoded audio samples, not whole-file hashes, when testing
audio parity.

The bridge action `42230` and native render action `41824` also produced
sample-identical output for the deterministic control. On the processed mix,
their residual remained at the plugin nondeterminism floor.

## State restoration and failure handling

- Captures wrote audio outside the project and did not add timeline items.
- Render settings and time selection were read back after each run and restored.
- The bridge verifies that every returned render target maps to one expected
  track or the master; it refuses ambiguous or incomplete mappings.
- A render can block the deferred bridge loop and outlive the client's timeout.
  Blindly retrying may issue a second render and trigger an overwrite dialog.
  After a timeout, inspect bridge/project state before any retry.

These results support using REAPER's native render path for measured track and
master audio. They do not prove that a candidate mix is good, or that any render
from a different project/plugin chain will match exactly.
