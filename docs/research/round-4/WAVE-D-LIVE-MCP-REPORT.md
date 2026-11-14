# Wave D live MCP capture report

**Run date:** 2026-11-12 (local time)  
**REAPER:** 7.84/x64  
**MCP surface:** current checkout over MCP stdio; 19 tools discovered  
**Live fixture:** disposable synthetic project, two tracks, 2.0 s, 48 kHz

## Full-project render tail — verified

The MCP call used no explicit section and requested a 500 ms tail. REAPER rendered
three sources: Guitar, Bass and master. The bridge reported a 120,000-frame file
at 48 kHz and a 2.0 s project length. The library resolved the measured project
window as 96,000 frames / 2.0 s and kept the 24,000-frame / 0.5 s tail in the
captured file only.

| Check | Result |
|---|---|
| `analysis_windows.measured` | 0.0–2.0 s |
| `analysis_windows.captured_file` | 0.0–2.5 s |
| Tail included in measurements | `false` |
| Boundary evidence | 120,000 file frames − 24,000 tail frames = 96,000 project frames |
| Sources | 3: two tracks plus master |
| Audio resources | 3/3 read; stereo, 48 kHz, 120,000 frames, 2.5 s each |
| Asset integrity | All three resource SHA-256 values matched the capture records |
| `reaper_release` | `released=true`; the job directory was removed and reads after release failed |
| Project integrity | Same track/item IDs and positions; `.RPP` SHA-256 unchanged |

The project boundary is accepted only when the render tail, audio frame count and
REAPER project length agree within one sample frame. If the boundary is absent,
invalid or inconsistent, the capture fails closed as unknown. Unit regressions
also exercise those failure cases and prove that a deliberately loud tail does
not affect metrics over the project window.

## Tests

| Suite | Result |
|---|---|
| Complete suite, Python 3.13.17 | `760 passed, 2 skipped, 3 warnings` in 14.90 s |
| Complete suite, Hermes Python 3.14.9 | `760 passed, 2 skipped, 3 warnings` in 16.01 s |
| Focused boundary/MCP tests, Hermes Python 3.14.9 | `64 passed, 3 warnings` |

The warnings are SciPy adjusting its FFT segment length for short synthetic
test signals; no test failed.

## Earlier section capture

An earlier live MCP run on the disposable `bastard_edit_ogg.RPP` fixture used an
explicit 0–1 s section and 250 ms tail. It returned 11 tracks plus master; all
12 resources read as stereo, 48 kHz, 1.25 s, while measurements covered only
0–1 s. That run predates the full-project-boundary fix. The current synthetic
full-project run separately verifies the no-section case.

## Scope and limits

- The live no-section proof uses a synthetic 2-second project; it verifies the
  REAPER/MCP boundary, audio resource metadata and cleanup, not full-song mixing.
- The actual audio-level exclusion of a deliberately loud tail is covered by a
  unit test; the live proof uses matching frame counts and project length.
- No producer reference mix was opened or compared. The HiMMP autonomous mix and
  same-song reference comparison remain separate pending work.
- A separate diagnostic attempt on the 5-minute fixture was canceled after its
  sequential full analysis continued for several minutes. Its manifest-owned
  temporary audio job was cleaned through the library cleanup routine. This run
  does not establish a general performance regression or a completion time for
  the default parallel path.
