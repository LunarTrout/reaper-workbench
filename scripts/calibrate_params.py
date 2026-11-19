"""Measure normalized→displayed mappings so edits can target real values.

Usage:
    python scripts/calibrate_params.py --track GUID --fx GUID --params 1,13,14
                                       [--project-id ID] [--archive-root PATH]
                                       [--points 11]

For each parameter the script sets a small sweep of normalized values, records
what the plug-in displays, restores the original value and stores the table in
``<archive>/state/plugin_summaries`` so ``fx.set_parameter`` accepts
``"target": "25 ms"`` instead of a raw normalized number.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reaper_audio.context.plugin_state_store import PluginStateStore
from reaper_audio.reaper.bridge_client import ReaperBridge
from reaper_audio.reaper.handles import ReaperHandle


def _handle(bridge: ReaperBridge, project_id: str, track_guid: str) -> ReaperHandle:
    change_count = bridge.health()["project"]["change_count"]
    return ReaperHandle(kind="MediaTrack", project_id=project_id,
                        change_count=change_count, stable_id=track_guid)


def _read(bridge: ReaperBridge, project_id: str, track_guid: str,
          fx_index: int, index: int) -> tuple[float, str]:
    handle = _handle(bridge, project_id, track_guid)
    normalized = float(bridge.call_api("TrackFX_GetParamNormalized", handle,
                                       fx_index, index).returns[0])
    result = bridge.call_api("TrackFX_GetFormattedParamValue", handle,
                             fx_index, index)
    formatted = str(result.returns[1]) if len(result.returns) > 1 else str(result.returns[0])
    return normalized, formatted


def _set(bridge: ReaperBridge, project_id: str, track_guid: str,
         fx_index: int, index: int, value: float) -> None:
    bridge.call_api("TrackFX_SetParamNormalized",
                    _handle(bridge, project_id, track_guid), fx_index, index,
                    float(value))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--track", required=True)
    parser.add_argument("--fx", required=True)
    parser.add_argument("--params", required=True)
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--points", type=int, default=11)
    args = parser.parse_args()

    bridge = ReaperBridge()
    context = bridge.project_context(args.project_id, include_fx_params=False)
    track = next((row for row in context.get("tracks") or []
                  if row.get("guid") == args.track), None)
    if track is None:
        raise SystemExit("track not found in the project")
    fx_row = next((row for row in track.get("fx") or []
                   if row.get("guid") == args.fx), None)
    if fx_row is None:
        raise SystemExit("plug-in not found on the track")
    fx_index = int(fx_row["index"])

    listing = bridge.call("fx.list_params",
                          {"track_guid": args.track, "fx_guid": args.fx})
    names = {int(row["index"]): str(row.get("name")) for row in listing["parameters"]}
    store = PluginStateStore(args.archive_root)

    for raw in args.params.split(","):
        index = int(raw.strip())
        if index not in names:
            print(f"parameter {index}: not present on {fx_row['name']}")
            continue
        original, original_text = _read(bridge, args.project_id, args.track,
                                        fx_index, index)
        table = []
        steps = max(3, args.points)
        for step in range(steps):
            normalized = step / (steps - 1)
            _set(bridge, args.project_id, args.track, fx_index, index, normalized)
            _, text = _read(bridge, args.project_id, args.track, fx_index, index)
            table.append({"normalized": round(normalized, 6), "formatted": text})
        _set(bridge, args.project_id, args.track, fx_index, index, original)
        store.put_calibration(args.project_id, args.track, args.fx, index,
                              name=names[index], table=table)
        print(f"{index} {names[index]} (was {original_text}): "
              + " | ".join(f"{row['normalized']:.2f}={row['formatted']}" for row in table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
