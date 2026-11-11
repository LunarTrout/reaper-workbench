# Safety and project-state guarantees

This document defines what the library currently protects and where callers must supply additional checks. It is not a guarantee that every REAPER API function or Action is safe to automate.

## Trust boundary

- REAPER remains the host of project objects and audio processing. The Lua bridge runs inside REAPER and invokes ReaScript APIs or Actions.
- The Python client and MCP server communicate with the bridge through files under the current Windows user's `%LOCALAPPDATA%`. No network port is opened by this transport.
- Any local process with permission to write to that runtime directory may be able to submit commands to the bridge. Keep the user profile private; do not redirect the runtime directory to a shared or network location.
- An MCP client can receive project context and leased audio, and can issue editing calls. The client/operator is responsible for deciding which model provider may receive audio or project information.

## Context binding

Project edits should be bound to all of the following:

- bridge session ID, to select the intended REAPER process;
- project ID, to select the intended open project;
- expected revision, to detect changes since the model inspected the project;
- object GUIDs, to resolve tracks, items, FX, and other objects inside REAPER.

Track indices are snapshot-local and must not be retained as identities. If the project revision changes between preview and apply, discard the plan, reread context, and build a new plan.

## Typed edit transactions

For supported operations, use the `reaper_edit` preview/apply/undo sequence. Preview is non-mutating and returns a plan bound to the project's current revision. Apply rejects stale plans and returns the post-apply revision. Read back every touched object. Undo is allowed only when the supplied revision is the exact revision returned by the target plan and no newer edit has superseded it.

The typed transaction set is narrower than the whole ReaScript API. Generic `reaper_api_call` and `reaper_action` can invoke operations outside that set. They may save or close projects, write preferences, affect hardware or plugins, create/delete files, or alter global state. Their effects are not automatically constrained by the typed transaction layer. Discover signatures and side effects before calling them, then inspect the resulting project and external state.

## Saving, rendering, and media

- Saving is explicit. `save_as` checks the target path and does not overwrite an existing file unless the caller passes the explicit overwrite option.
- Rendering and audio capture create artifacts; deleting those artifacts does not make the render itself reversible.
- Analysis files live outside the `.RPP` in a per-job directory. Cleanup is limited to library-owned paths listed in that job's manifest. Do not manually delete a path named in a cleanup failure before checking its manifest and ownership.
- The analysis capture temporarily changes REAPER render settings and target selection. The bridge restores them and returns the new revision. The internal project change counter may increase; callers must use the returned revision, not reset it or reload the project.
- Project analysis uses offline render outputs and a separate master output. `GetAudioAccessorSamples` returns pre-FX data and is not interchangeable with rendered track output.
- The library does not yet provide a complete original/final project version manager. Keep a separate backup before making important edits.

## Audio and model-provider privacy

The bridge and analysis library process audio locally. However, an MCP client can transmit returned audio resources and project context to a remote model provider. Review that client's privacy and retention settings before enabling audio access. The library cannot enforce what a provider stores after receipt.

Temporary audio is leased and cleaned on release or by stale-job cleanup. A crash can leave a job directory until a later cleanup pass. Retention policy is designed to avoid accumulating analysis stems; verify the cleanup result when a job reports an error.

## Operational practice

1. Use a duplicate project for first use or unfamiliar edits.
2. Confirm the bridge session and project identity before making changes.
3. Read the current project revision and enough context to identify the target.
4. Use typed preview/apply for supported edits; use generic API/Actions only after inspecting their semantics.
5. Read back changes and confirm undo state before attempting recovery.
6. Save only when requested and to an explicitly chosen path.

This pre-1.0 build has explicit limits. These controls reduce specific classes of mistakes; they do not guarantee that a model will make the right musical decision or that every operation can be rolled back.
