# REAPER Workbench

**A Python library for context-aware REAPER control and audio analysis, with an optional MCP server for AI clients.**

Use it to inspect a live REAPER project, capture its tracks and master for analysis, compare audio, and make project edits through the documented ReaScript API. The library exposes project context and low-level capabilities; it does not prescribe a mixing or composition recipe.

[Русская версия](README.ru.md) · [Installation](docs/INSTALLATION.md) · [Architecture](docs/ARCHITECTURE.md) · [MCP guide for models](docs/MCP-README-LLM.md) · [Validation status](docs/VALIDATION.md)

> **Release status: pre-1.0.** The public API and MCP surface may change before the first stable release. Validation evidence and known limits are documented below; a passing unit suite does not imply complete ReaScript coverage or production readiness.

## Scope

The project has three layers:

| Layer | Responsibility |
|---|---|
| **REAPER bridge** | A resident Lua ReaScript executes requests inside REAPER through its scripting API and Actions. |
| **Python library** | Project context, typed API discovery, audio capture, measurements, comparisons, and revision-bound edits. |
| **MCP server** | An optional stdio interface that presents project-aware tools and resources to an MCP client. |

The bridge and Python client communicate through local request and response files. They do not use Frida, inject into REAPER, or inspect another process's private memory. The bridge currently relies on Windows `LOCALAPPDATA`; Windows is the supported development platform. Other operating systems have not been validated.

## Capabilities

### Project context and control

- Read project identity and revision, tempo map, tracks, master, routing, sends, items, takes, FX chains, and selected envelope or MIDI details.
- Identify project objects by GUID and bind calls to a project revision, rather than relying on track indices that may change.
- Search a bundled catalog of **732 documented ReaScript functions** (catalog generated from the official v7.85 reference), inspect signatures, and issue typed API calls. Runtime availability still depends on the installed REAPER version.
- Search and invoke REAPER Actions for operations that are available only through the Action List.
- Prepare supported edit batches, preview their intended targets and values, apply them against an expected revision, read back the result, and undo the latest transaction where REAPER permits it.

The catalog is a discovery surface, not a claim that every API function has been exercised or has a safe transaction wrapper. Generic API calls and Actions are low-level operations; they are not automatically covered by the library's preview/undo guarantees.

### Audio analysis

- Capture separate track render outputs and a separate master output through REAPER's offline render mechanism.
- Attach source identity, project revision, time range, render provenance, and available mixer context to the analysis report.
- Run the integrated measurement suite for the captured sources: level and loudness, dynamics, spectral balance, stereo and phase relationships, transients, temporal profiles, tempo context, and source/reference comparisons.
- Compare a project source with another file or a selected time window from a reference file.
- Produce structured mix evidence and edit evidence from measurements without encoding a fixed order of operations.

REAPER's Audio Accessor is a different signal tap: its samples are pre-FX and must not be described as processed mix stems. The current project-analysis path uses REAPER rendering. Capture details and known limits are documented with the validation status; never infer signal stage from a filename alone.

Analysis audio is stored outside the `.RPP` in a bounded job directory. It is leased for the analysis request and cleaned up on release or by the stale-job policy. The capture path may temporarily change render settings or track selection and restore them afterward; callers must use the returned revision rather than assuming the project's change counter is unchanged.

### LLM-facing interface

The MCP server currently exposes a compact set of **12 tools** and **5 resource templates**. It does not publish one tool per ReaScript function. The model can inspect project context, browse capability domains, search an operation by structured object/operation qualifiers, request integrated analysis, prepare and apply supported edit batches, and call lower-level API functions or Actions when needed.

The model-facing contract is intentionally broader than preset musical workflows. Mixing, editing, arranging, MIDI authoring, and other tasks are expressed as goals assembled from project inspection, analysis, and REAPER operations; the interface does not impose genre rules or a fixed processing chain. See the [model guide](docs/MCP-README-LLM.md) for tool semantics and context handling.

## Installation and connection

The project currently targets Windows, Python 3.13+, and a running REAPER installation. The Lua bridge is loaded by REAPER; Python or MCP clients run alongside it.

1. Install the Python package and copy the bridge Lua files into a dedicated folder under REAPER's resource-path `Scripts` directory.
2. Load `reaper_bridge.lua` from REAPER's Action List. Keep `lua_json.lua` beside it; the bridge runs as a deferred ReaScript.
3. Start the stdio MCP server from the same Python environment and configure your MCP client to launch `python -m reaper_audio.mcp_server`.
4. Start with `reaper_session(action="health")`, then read the project context and revision before making changes.

Full commands, client configuration, troubleshooting, and bridge lifecycle instructions are in [Installation](docs/INSTALLATION.md). The REAPER ReaScript API and Action List are documented by [Cockos](https://www.reaper.fm/sdk/reascript/reascript.php).

## Reliability model

Project-aware calls use stable identifiers and expected revisions. A supported edit is not considered successful merely because REAPER accepted a command: inspect the post-edit context and confirm the touched objects. If the project has changed since the plan was prepared, the plan is stale and must be rebuilt from fresh context.

This protection has defined limits. A generic ReaScript call or Action may have side effects beyond the typed transaction layer and may not be safely undoable by the library. Saving, overwriting, rendering, destructive editing, and project-file management require their own explicit checks. The safety model and current gates are documented in [Safety](docs/SAFETY.md) and [Validation status](docs/VALIDATION.md).

## Development

~~~powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,mcp]"
python -m pytest
~~~

Run the MCP server with:

~~~powershell
python -m reaper_audio.mcp_server
~~~

The server speaks MCP over stdio. It expects the REAPER bridge to be running in the instance that should be controlled. For API catalog maintenance, use `scripts/generate_api_catalog.py`; the generator reads the official Cockos reference and records its source version and content hash.

## Documentation

- [Installation and connection](docs/INSTALLATION.md)
- [Architecture and data flow](docs/ARCHITECTURE.md)
- [Safety and project-state guarantees](docs/SAFETY.md)
- [MCP guide for language models](docs/MCP-README-LLM.md)
- [Validation status and release gates](docs/VALIDATION.md)
- [Russian documentation](README.ru.md)

## License

No license has been selected for this repository yet. Until a license file is added, all rights remain reserved; do not assume that the source is available for reuse.
