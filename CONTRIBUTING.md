# Contributing

This project is pre-1.0. Changes should preserve explicit context, provenance, and failure behavior; avoid adding musical rules or a fixed production workflow to the library surface.

## Before opening a change

- Describe the behavior and its REAPER/API assumptions.
- Keep project and audio data out of commits. Never attach a user's `.RPP`, media, renders, or local REAPER profile.
- Do not add credentials, machine-specific absolute paths, or generated runtime artifacts.
- For a new typed edit, define target identity, revision preconditions, before-state capture, validation, apply, readback, and undo behavior.
- For analysis changes, define units, time reference, signal stage, coverage, and failure cases.
- Update English and Russian user-facing docs together when public behavior changes.

## Local checks

~~~powershell
python -m pip install -e ".[dev,mcp]"
python -m pytest -q
~~~

Live REAPER checks must use a disposable project copy and state the REAPER version, bridge session, operation, observed result, and cleanup result. Do not validate destructive operations against a production project.

## Pull requests

Keep changes focused. Include what changed, why it is needed, the checks run, and any live-REAPER coverage or remaining limitations. Do not describe an API catalog entry as tested unless it was exercised in REAPER.
