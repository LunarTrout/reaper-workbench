# Music Flamingo — A/B proof that the captured master audio is complete and correct

Date: 2026-11-11. Project used for the test: a byte-identical copy of the user's working
mix, `<local-projects>/vers1_reaper/vers1_abtest.RPP`
(md5 `5385ef2b7a36b1ef41de766763942512`, identical to `vers1_mix_20261111.RPP`;
the copy was never saved, so the file on disk stayed unchanged).

REAPER 7.84, bridge session, 33 tracks, 65 media items, 5-point tempo map.
Render target: master mix, 44.1 kHz, 2 channels.

## 1. What was compared

Two independent paths, same open project and revision:

| Path | Mechanism |
| --- | --- |
| Native | `GetSetProjectInfo` render settings + `Main_OnCommand(41824)`, REAPER's own "Render project" |
| Capture | `project.capture_render(master_only=True)` over the bridge |

`RENDER_BOUNDSFLAG` semantics used here: `1` = entire project, `2` = time selection
(`0` would be custom time bounds with explicit `RENDER_STARTPOS/ENDPOS`).
An earlier attempt used `1` for a 10 s section and REAPER correctly rendered the whole
project (5:08.987, 81,758,700 bytes) — that run is void and is not part of the result.

Settings equalised for both paths: master mix (`RENDER_SETTINGS=0`), 44.1 kHz,
2 channels, no normalise, no dither, no render tail, no "add to project".

## 2. Section 30.0–40.0 s (boundsflag 2)

| | native | capture |
| --- | --- | --- |
| frames | 441 000 | 441 000 |
| sample rate | 44 100 | 44 100 |
| channels / subtype | 2 / PCM_24 | 2 / PCM_24 |
| peak | −4.364 dBFS | −4.365 dBFS |
| RMS | −18.642 dBFS | −18.643 dBFS |
| bytes | 2 646 690 | 2 646 690 |
| sha256 (first 16) | 06fb28870c1736d9 | 82e876ed9b8b0278 |

Null (sample-aligned subtraction): residual peak −23.67 dBFS, residual RMS −47.32 dBFS,
depth −28.67 dB. Length delta 0 frames.

## 3. Full project (boundsflag 1)

| | native | capture |
| --- | --- | --- |
| frames | 13 626 335 | 13 626 335 |
| duration | 308.987 s | 308.987 s |
| peak | −0.497 dBFS | −0.160 dBFS |
| RMS | −18.108 dBFS | −18.108 dBFS |
| bytes | 81 758 700 | 81 758 700 |

Null: residual RMS −38.31 dBFS, depth −20.20 dB, length delta 0 frames.

## 4. Why the residual is not a capture defect

Two renders through the *same* native path, same range, same settings:

* native vs native: residual RMS **−46.82 dBFS**, depth −28.18 dB.

The native-vs-capture residual (−47.32 dB) is the same magnitude as REAPER against
itself, so the difference is produced by the project, not by the capture path.
The cause is analog-modelled plugins (Fortin Nameless amp sims on the guitars, Kontakt
instruments on the accordion tracks) that are not sample-deterministic between renders.

### Deterministic control

Isolating a single dry track without any FX (`аккордеон 1`, one item 0–74.09 s) with every
other track muted through one `reaper_edit` plan, then rendering the same 10 s three times:

| comparison | frames | signal | residual | depth |
| --- | --- | --- | --- | --- |
| native vs native | 441 000 / 441 000 | −33.12 dBFS | −240.00 dBFS | −206.88 dB |
| native vs capture | 441 000 / 441 000 | −33.12 dBFS | −240.00 dBFS | −206.88 dB |

Residual −240 dBFS is the float floor: the difference array is exactly zero. With no
non-deterministic plugin in the path, the capture render is **sample-exact** identical to
REAPER's own render. Mutes were restored (`mutes restored: True`), the project file on
disk stayed byte-identical.

`sha256` of the WAVs still differs because REAPER writes BWF metadata (time reference /
UMID) that is not part of the audio payload; the audio itself nulls to zero.

## 5. Invariants checked

* Output WAVs live outside the project: `%LOCALAPPDATA%/hermes/cache/scratch/...`.
* No item was added: 65 media items before and after the capture runs; the project folder
  still contains zero `.wav` files.
* Render settings restored: read-back drift `{}` for every numeric `RENDER_*` field;
  time selection restored ([30, 40] before and after). The harness's own string readback
  used the wrong return order — `GetSetProjectInfo_String` returns `{ok, value}`, not
  `{value, ok}` — so `RENDER_FILE`/`RENDER_PATTERN` were left pointing at a scratch dir by
  the test scripts and were reset to the project folder and `$project` afterwards;
  `capture_render` snapshots and restores both strings correctly by itself.
* `capture_render` restores `DIRTY` itself (`reaper_bridge.lua`, `restore()`), verifies
  every `RENDER_*` value it wrote, and refuses targets outside `output_dir`.
* Our own control edits (mute plan) left the copy dirty in memory, which is why the test
  project shows the modified flag; the file on disk is unchanged and unsaved.

## 6. Action equivalence: 42230 (bridge) vs 41824

`project_capture_render()` renders with `Main_OnCommandEx(42230, 0, project)`, so the
native control was re-run with exactly that call against the same project object.
Deterministic control (one dry FX-free track, 30–40 s, mutes restored and verified):

| comparison | frames | residual RMS | depth |
| --- | --- | --- | --- |
| native 41824 vs native 42230 | 441 000 / 441 000 | −240.00 dBFS | −206.88 dB |
| native 42230 vs capture_render | 441 000 / 441 000 | −240.00 dBFS | −206.88 dB |

All three renders are sample-exact identical: the two action IDs are equivalent here,
and the capture path matches the bridge's own action.

Re-run of the section with 42230 on the full mix: 441 000 frames both ways, peak
−4.808 / −4.808 dBFS, RMS −19.754 / −19.752, null −46.75 dBFS (the nondeterminism floor).

Full-length case with 42230: 13 626 335 frames both ways (81 758 700 bytes, PCM_24),
peak −1.571 / −1.676 dBFS, RMS −19.071 / −19.073, null −38.22 dBFS, length delta 0.
Render settings drift after both runs: none; time selection [30, 40] restored.

Note: absolute levels between REAPER sessions differ by ~1.1 dB (a fresh process reloads
the same project with slightly different plugin state). A/B comparisons are therefore only
valid inside one session at one revision, which is how every run above was done.

Pitfall found while doing this: `Main_OnCommandEx` returns `bridge_timeout` because the
render blocks the bridge defer loop, and the library's retry re-issues the command — the
second render hits an existing file and REAPER opens a modal "Render Warning: overwrite
files", which then blocks the bridge indefinitely. Issue the render command exactly once
and dismiss/inspect that dialog by title.

## 7. Music Flamingo / GPU status

The V100 on this install (C:, a WinToUSB Windows, build 28000) never starts: device
problem code 10 → `0xC000009A`, `nvlddmkm` service stopped with exit code 31, no PCI
resources assigned, and no `HardwareInformation.MemorySize` ever written by the driver.
The driver image and package are intact and RAM is not the limit (47 GB total, 26 GB free).

Comparing the two installs read-only (offline SYSTEM hive of D:) found the concrete
difference: **D: runs the Tesla-class `nvgridsw.inf` package, 32.0.15.8253 (582.53)**,
while C: ran the consumer `nv_dispwi.inf` package, 32.0.15.8299 (582.99). The GRID package
was added to C: with `pnputil /add-driver ...\nvgridsw.inf /install` (installed as oem6)
and the consumer package removed with `pnputil /delete-driver oem15.inf /uninstall /force`.
The device now binds 32.0.15.8253 and reports problem code **43** instead of 10 — the
driver loads, the device still fails post-start, and `nvidia-smi`/`--list-devices` remain
negative. A reboot is required to reinitialise the display stack; until then inference
stays fail-closed.

Rollback: `pnputil /add-driver` the consumer package again (the installer is
`nvidia-driver-582.99.exe` in `model-store/moss-music-8b-q8`) and remove oem6.

Model status:

* Weights: Q5_1 `0742a216…51a6` (5 754 852 544 B), mmproj `d4be69ed…4f94`
  (1 324 506 912 B), revision `a059053433697011c6928b1962110040f4bcb4d0`.
* llama.cpp `b12284` CUDA 12.4 x64, `llama-mtmd-cli.exe` + `mtmd.dll`.
* `--list-devices` currently reports `Available devices: (none)` plus
  `ggml_cuda_init: failed to initialize CUDA: no CUDA-capable device is detected`.
  Parsed as failure (`saw_none`, `init_failed`), so `require_gpu()` fails closed and no
  audio is ever sent to the model.
* Inference additionally fails closed unless llama.cpp logs
  `offloaded N/M layers to GPU` with N > 0 — a run that silently fell back to CPU would
  raise `GPU_REQUIRED` instead of returning text.
* Cost: a fresh `llama-mtmd-cli` process costs a full model load (5.75 GB + 1.32 GB
  projector); a warm page-cache read of the GGUF alone measured 931 ms, so one process per
  window per representation would dominate runtime. Defaults are therefore one
  representation (`mono_sum`) per window; every extra view is an explicit argument.
