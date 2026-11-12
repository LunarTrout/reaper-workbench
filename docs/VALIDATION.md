# Validation status

This page distinguishes implementation that has been exercised from coverage that is still open. It is not a release certification.

## Confirmed

- The Python unit suite passes against the current source: 179 tests
  (`PYTHONPATH=src python -m pytest tests -q`). The count grows with the corpus
  module; treat the run, not this line, as the source of truth.
- The MCP server completed a stdio handshake, advertised 19 tools and 5 resource templates, and answered health and project-context requests against REAPER 7.84.
- A live MCP analysis request captured a five-second window for 11 track sources and the separate master, attached project-tempo context, and returned a releasable analysis record. `reaper_release` removed the associated temporary audio directory.
- A revision-bound item split was exercised against an isolated REAPER copy; apply changed the item count as planned and undo restored the original item count and geometry.
- Native capture stores temporary render files outside the project file. The capture restores project render settings and selection; REAPER's monotonic change count may nevertheless advance, and the bridge returns the post-capture revision.

## Still open

- Exercise the entire edit transaction through MCP transport: preview, apply, independent readback, stale-revision rejection, and undo.
- Confirm that a real MCP client can consume the audio resource format and deliver the audio to its model in the expected way.
- Expand live checks across the typed edit domains, especially FX, sends, MIDI, automation, project save, track/item lifecycle, and failure recovery.
- Verify capture signal stage for representative routing, FX chains, track/master outputs, tails, and REAPER versions. Pre-FX AudioAccessor output must remain distinct from rendered outputs.
- Validate the install and bridge lifecycle on a clean Windows setup, then define support for other operating systems.
- Complete project version management and confirm the intended original/final retention behavior.
- Audit dependency and bundled-file licensing, select a repository license, and run clean-environment packaging and CI checks.

## Release gate

Do not call this production-ready until the clean install, MCP lifecycle, representative project operations, capture provenance, cleanup, and recovery behavior have passed reproducible checks. A passing unit suite does not establish full ReaScript API coverage or correct musical decisions.

The live integration checks use isolated REAPER sessions. They do not touch a user's active production project. This page omits local project paths and media names by design.
