# Bridge lifecycle: activating a new resident script without touching the project

The resident bridge is a ReaScript inside REAPER. The Python side cannot restart
it the way it restarts its own process, so a library fix that lives in
`scripts/reaper_bridge.lua` stays inert until REAPER boots that file again. A
gateway restart reloads Python only.

This document defines the narrow contract that makes an activation safe,
provable and observable. It is deliberately *not* a generic "run this code" hook.

## What the bridge reports

| Field | Where | Meaning |
|---|---|---|
| `bridge_build` | `health`, instance registry | build constant of the copy that is actually running |
| `bridge_session` | `health`, registry, every projection | one boot = one session id |
| `script_path` | `health` | the file this copy was booted from |

`bridge_build` lives in the script as `local bridge_build = "..."` so the caller
can read the *intended* build from disk and compare it with the *observed*
running build. Nothing is inferred from mtimes or file sizes.

## The lifecycle operation

`bridge.lifecycle` accepts exactly one action, `reload`, and requires
`expected_build`:

1. `expected_build` must equal the running `bridge_build`, otherwise the call
   fails `stale_context` — a caller that read an older state cannot force a
   reload.
2. The script on disk is compiled with `loadfile` **before** anything stops.
   A file that does not compile returns `reload_rejected` and the running loop
   keeps serving: a broken edit never kills the only bridge.
3. The file must be readable and implausibly-small files are refused.
4. Only then the loop answers the request, sets `pending_reload`, and on its
   next turn calls the compiled chunk. The chunk starts the new resident loop
   and returns; the old loop ends without rescheduling.

The operation never saves, closes, reloads or edits a project. It reads and
executes this one file, by the path the running script itself reported.

## Why a reload cannot leave two loops

`dofile`-style boots share one Lua global table. Every boot publishes
`_G.reaper_bridge_supersede = <session>:<token>` and every loop compares that
token before it reschedules. The newest boot owns the mailbox; an older loop
that wakes up later sees a foreign token and returns without rescheduling.

Combined with the reload path (old loop stops, then the chunk starts the new
one), a reload converges to exactly one resident loop. The Python client also
verifies it from the outside: `activate_bridge` fails with
`duplicate_sessions` when more than one live session is visible after the
reload.

## What the client proves before it reports success

`reaper_bridge_reload` (MCP) → `activate_bridge` (`reaper_audio.reaper.bridge_lifecycle`):

| Situation | Result |
|---|---|
| running build == disk build | `reloaded: false`, `reason: already_current` |
| running bridge reports no build | `bootstrap_required`; no action bootstrap is attempted |
| on-disk script declares no build | `disk_build_missing` |
| bridge refuses (`reload_rejected`, `stale_context`) | `reload_refused` with the bridge code |
| no new session with the expected build before timeout | `new_session_missing` |
| more than one live session after the reload | `duplicate_sessions` |
| active project id changed during the activation | `project_changed` |
| new session reports the expected build | `reloaded: true` + previous/new session, build, project id, elapsed |

Every failure is an exception, never a partial success.

## Cold start when the bridge is absent

The MCP lifecycle tool cannot call ReaScript APIs when there is no resident
bridge: there is no RPC endpoint to receive a bootstrap request. In that
state, use REAPER's command-line script handoff to run the exact bridge file in
the already-running REAPER process. The same handoff is the documented recovery
for a legacy bridge that returns `bootstrap_required`. The REAPER CLI supports passing a
`scriptfile.lua`; `-nonewinst` routes it to an existing instance instead of
starting another one ([REAPER CLI reference](https://github.com/ReaTeam/Doc/blob/master/REAPER-CLI.md)).

On Windows, from PowerShell:

```powershell
& "C:\Program Files\REAPER (x64)\reaper.exe" -nonewinst "C:\path\to\reaper_bridge.lua"
```

Use the executable path of the live REAPER process and the configured absolute
bridge path. Pass no `.RPP` path and do not close or restart REAPER. This is a
service bootstrap only; it does not read or edit project content. Use it when
health/discovery confirms there is no live bridge, or after a legacy bridge
returns `bootstrap_required`. For a bridge that advertises the lifecycle
contract, use `reaper_bridge_reload` instead of starting another instance.

After the command returns, success is not inferred from its exit code. Verify
through MCP that exactly one bridge session is live, its `bridge_build` matches
the on-disk build, and its project identity matches the project already open in
REAPER. If that proof fails, stop before capture or edits.

## Why action-based bootstrap is disabled

A legacy bridge does not advertise `bridge_build` or `bridge.lifecycle`, so the
MCP lifecycle tool returns `bootstrap_required`. It does not expose an option to
run `Main_OnCommandEx` synchronously: that path can terminate the old deferred
bridge before a new endpoint is confirmed. Use the `-nonewinst` handoff above,
then prove through MCP that exactly one session is live, its build matches the
installed script, and the active project identity is unchanged. Starting
another instance of the same ReaScript ends its previous instance; the
supersede token covers only an in-place reload within one Lua state.

Idempotency: when the running build already equals the disk build the call is a
no-op (`reloaded: false`, `reason: already_current`) — it never boots again.

Do not restart REAPER or reload a project as part of normal bridge activation.
If the `-nonewinst` handoff or its MCP verification fails, stop and report the
exact failure instead of trying a synchronous action, `Close All ReaScripts`,
or a blind project/app restart.
