from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generate_api_catalog import build_catalog  # noqa: E402


def test_catalog_generator_keeps_official_descriptions_and_all_language_signatures() -> None:
    html = """
    <a name="SplitMediaItem"><hr></a><br>
    <div class="c_func"><span>C: </span><code>MediaItem* SplitMediaItem(MediaItem* item, double position)</code></div>
    <div class="e_func"><span>EEL2: </span><code>MediaItem SplitMediaItem(MediaItem item, position)</code></div>
    <div class="l_func"><span>Lua: </span><code>MediaItem reaper.SplitMediaItem(MediaItem item, number position)</code></div>
    <div class="p_func"><span>Python: </span><code>MediaItem RPR_SplitMediaItem(MediaItem item, Float position)</code></div>
    Splits the item at position.<br>
    NULL is returned if the split fails.<br><br>
    <a name="GetTrack"><hr></a><br>
    <div class="c_func"><span>C: </span><code>MediaTrack* GetTrack(ReaProject* proj, int trackidx)</code></div>
    <div class="e_func"><span>EEL2: </span><code>MediaTrack GetTrack(ReaProject proj, int trackidx)</code></div>
    <div class="l_func"><span>Lua: </span><code>MediaTrack reaper.GetTrack(ReaProject proj, integer trackidx)</code></div>
    <div class="p_func"><span>Python: </span><code>MediaTrack RPR_GetTrack(ReaProject proj, Int trackidx)</code></div>
    Get a track by zero-based index.
    """

    catalog = build_catalog(html)
    split = catalog["functions"]["SplitMediaItem"]

    assert catalog["function_count"] == 2
    assert split["description"] == (
        "Splits the item at position.\nNULL is returned if the split fails."
    )
    assert split["eel"]
    assert split["python"]
    assert split["lua"]["parameters"][0]["type"] == "MediaItem"
