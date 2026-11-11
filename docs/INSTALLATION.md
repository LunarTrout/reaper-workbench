# Installation and connection

This guide installs the Python client and loads the resident REAPER-side Lua bridge. It then connects an MCP client over stdio. No REAPER plugin, distant API setting, socket listener, or Python integration inside REAPER is required.

## Requirements

- Windows. This is the only platform currently documented and validated for the bridge.
- REAPER with ReaScript enabled. The current live checks used REAPER 7.84; API availability varies by REAPER release.
- Python 3.13 or newer for the external library and MCP server.
- An MCP client that can start a local stdio server, if MCP access is wanted.

REAPER embeds Lua for ReaScript. The Python process is separate and communicates with the resident script through a local file mailbox; Python does not need to be enabled in REAPER's preferences and does not need to match REAPER's process bitness.

## 1. Get the source and install Python dependencies

Clone the repository, then open PowerShell in its directory:

~~~powershell
git clone https://github.com/LunarTrout/reaper-workbench.git
Set-Location reaper-workbench
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[mcp]"
~~~

For development and tests, install the development extra as well:

~~~powershell
python -m pip install -e ".[dev,mcp]"
python -m pytest
~~~

The `mcp` extra installs the optional MCP SDK. The project library can be used without the MCP server by installing the base package only.

## 2. Install the Lua bridge files

Keep the two bridge files side by side. The Lua script resolves its JSON helper relative to its own file location.

1. In REAPER, choose **Options → Show REAPER resource path in Explorer**. This opens the active resource directory, including for portable installations.
2. Open its `Scripts` directory. Create a subdirectory such as `REAPER-Workbench`.
3. Copy `scripts/reaper_bridge.lua` and `scripts/lua_json.lua` from this repository into that subdirectory.

Do not rename one file without updating the bridge's helper lookup. The bridge runtime and analysis artifacts are stored separately from the project file.

## 3. Start the resident bridge in REAPER

1. Open REAPER and the project/instance you intend the library to control.
2. Open the Actions window (normally `?`).
3. Choose **ReaScript: Load...**, select `reaper_bridge.lua`, and add it to the Action List.
4. Run the new action. The script remains active as a deferred ReaScript and services local requests.

The bridge uses the REAPER Lua runtime and the documented ReaScript API. It publishes instance discovery and request/response files under:

~~~text
%LOCALAPPDATA%\REAPER-Audio-Library\runtime\
~~~

The runtime directory keeps its original internal name for compatibility. It contains bridge mailbox and short-lived analysis artifacts, not project files or a growing stem archive.

The MCP server does not choose a project based on a filename. Query `reaper_session(action="health")` and `reaper_project` first. If multiple REAPER instances are bridged, use the returned bridge session ID on subsequent calls so a request is bound to the intended instance.

To stop the bridge, use REAPER's Actions window and stop the running deferred script. Cockos documents that deferred ReaScripts remain in the background until stopped from the running-scripts list: [ReaScript: running and stopping scripts](https://www.reaper.fm/sdk/reascript/reascript.php).

## 4. Configure an MCP client

The server communicates over standard input/output. Add an entry equivalent to this to the MCP client's server configuration; use the absolute path to the Python executable in the virtual environment:

~~~json
{
  "mcpServers": {
    "reaper-workbench": {
      "command": "C:\\path\\to\\reaper-workbench\\.venv\\Scripts\\python.exe",
      "args": ["-m", "reaper_audio.mcp_server"]
    }
  }
}
~~~

Client configuration file locations and schema vary. The invariant is the executable plus `-m reaper_audio.mcp_server`; do not launch the server with a shell that writes banners or other text to stdout, because stdout is the MCP transport.

After connecting, use this order:

1. Call `reaper_session` with `action="health"` to confirm the bridge.
2. Call `reaper_project` to read project identity, revision, tracks, master, routing, and tempo context.
3. Use `reaper_capabilities` or `reaper_api_search` to discover an operation before making a low-level call.
4. For a supported edit, preview the revision-bound plan, apply it, read back the touched objects, and undo only when the returned revision still identifies that transaction as the newest edit.

See [MCP-README-LLM.md](MCP-README-LLM.md) for the model-facing contract and tool semantics.

## Audio and project files

Project analysis uses REAPER's offline render capture for separate source outputs and a separate master output. Temporary WAV files are kept outside the `.RPP` in the library's job directory and are removed by `reaper_release` or stale-job cleanup. They are not inserted as playlist tracks and are not intended to accumulate as project versions.

AudioAccessor data is a separate pre-FX tap; it must not be substituted for processed track renders. Capture temporarily changes render settings and selection as needed, restores those settings, and returns a new project revision. Refresh project context after capture; do not assume the change counter is unchanged.

The MCP client controls what happens to audio resources after it receives them. A client may forward audio to an external model provider according to its own configuration; this repository does not control that provider-side behavior.

## Troubleshooting

| Symptom | Check |
|---|---|
| `bridge_unavailable` | Confirm REAPER is open and the deferred `reaper_bridge.lua` script is running in the intended instance. |
| Lua error loading `lua_json.lua` | Confirm both Lua files are in the same folder and that REAPER loaded the intended copy of `reaper_bridge.lua`. |
| `LOCALAPPDATA` error | Launch REAPER in a normal Windows user session with the standard environment variables available. |
| API function unavailable | The catalog documents signatures; the installed REAPER version determines runtime availability. Search the catalog, then check the returned runtime result. |
| `stale_context` | Read the project again, inspect the changed context, and prepare a new plan. Do not replay an old plan against a new revision. |

## ReaScript reference

Cockos' [ReaScript guide](https://www.reaper.fm/sdk/reascript/reascript.php) documents the embedded Lua runtime, Action List loading, deferred scripts, and API reference. The version shown by REAPER's own **Help → ReaScript documentation** is authoritative for that installation.
