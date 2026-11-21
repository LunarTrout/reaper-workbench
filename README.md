# REAPER Workbench

[English](README.md) · [Русский](README.ru.md)

Python library and local MCP server for a live REAPER project: read the project,
measure real rendered audio, edit through typed batches, and keep one state
document that tells an agent what is known and what has gone stale.

The model never sees screenshots or a GUI. It talks to a resident ReaScript
bridge over a local file mailbox; REAPER can stay in the background.

## What it does

- **Project state.** One `state.json` per project holds the map, object
  fingerprints, measured facts and their freshness. `capture` and `measure`
  write it; status, freshness, coverage and context read it.
- **One context call.** `reaper_session(action="context")` returns the project,
  revision, compact map, health, facts summary, FX summary and changes.
  `since=<state_token>` returns only the delta; `pages=true` returns pages
  without a duplicate full view.
- **Measure, don't guess.** `reaper_measure` renders through REAPER and returns
  a sources × intervals × metrics table with LUFS, peaks, crest, bands,
  stereo and timing metrics, plus an evidence token and revision.
- **Measured facts without re-rendering.** `reaper_session(action="state")`
  reads what has already been measured, filtered by source, stage, window and
  metric. Stale facts stay marked stale.
- **Typed editing.** `reaper_edit` previews, applies and undoes track, item,
  take, FX, send, envelope, MIDI, marker, region and tempo changes. Every edit
  batch carries a receipt, readback and revision; apply/undo require a
  `journal.why`, stored append-only with its basis.
- **Async capture.** Per-source capture jobs report progress instead of
  blocking the control channel; offline media is woken before a render.
- **Delivery.** `reaper_media` imports references or encodes audio with a
  post-codec true-peak check.

The MCP surface is deliberately grouped: **41 tools in 11 groups** (`session`,
`measure`, `edit`, `midi`, `calibrate`, `analyze`, `action`, `api`, `flamingo`,
`spatial`, `media`). Call `reaper_capabilities(mode="tools")` for the current
list; `tools/list` is authoritative for schemas.

## Install

See [Installation](docs/INSTALLATION.md). Short version:

```powershell
git clone https://github.com/LunarTrout/reaper-workbench.git
Set-Location reaper-workbench
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[mcp]"
```

Load `scripts/reaper_bridge.lua` in REAPER (Actions → Load ReaScript, then Run).
It keeps a heartbeat and serves the Python client.

## MCP

```json
{
  "mcpServers": {
    "reaper-workbench": {
      "command": "python",
      "args": ["-m", "reaper_audio.mcp_server"],
      "env": { "PYTHONPATH": "src" }
    }
  }
}
```

At the start of a session read the resource `reaper://handbook` (or
`docs/HANDBOOK.md`). Measure/edit/midi/analyze refuse with `HANDBOOK_REQUIRED`
until it is read.

## Documentation

- [Handbook](docs/HANDBOOK.md) — the current operating contract · [Russian](docs/HANDBOOK.ru.md)
- [MCP guide for models](docs/MCP-README-LLM.md)
- [Installation](docs/INSTALLATION.md) · [Russian](docs/INSTALLATION.ru.md)
- [Policy](docs/POLICY.md) — thresholds belong to the owner
- [Safety](docs/SAFETY.md) — trust boundary and state guarantees
- [Partial loudness](docs/PARTIAL-LOUDNESS-GATE.md) — gate and residual method
- [Flamingo](docs/flamingo.md), [Ossicle](docs/ossicle.md) — optional listening backends

## Tests

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q
```

The suite is offline and uses mock bridges and synthetic audio; live REAPER
checks are separate.

## License and model weights

Model weights are not vendored. Flamingo weights are under NVIDIA OneWay
Noncommercial Research; review that licence before use beyond research.
