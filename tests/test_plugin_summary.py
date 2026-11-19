"""Compact plug-in summaries: mapping, staleness and persistence."""

from __future__ import annotations

from pathlib import Path

from reaper_audio.context.plugin_state_store import PluginStateStore, chain_fingerprint
from reaper_audio.context.plugin_summary import PluginDescriptor, summarize

PID = "reaper:{M}:C:/x/test.rpp"
TRACK = "{TRACK}"
FX = "{FX}"


def _param(index: int, name: str, formatted: str, normalized: float = 0.5) -> dict:
    return {"index": index, "name": name, "formatted": formatted,
            "normalized": normalized}


def _pro_q() -> list[dict]:
    params = [
        _param(0, "Band 1 Used", "Used", 1.0),
        _param(2, "Band 1 Frequency", "90.0 Hz", 0.274),
        _param(3, "Band 1 Gain", "0.00 dB", 0.5),
        _param(5, "Band 1 Shape", "Low Cut", 0.222),
        _param(23, "Band 2 Used", "Used", 1.0),
        _param(25, "Band 2 Frequency", "320.0 Hz", 0.433),
        _param(26, "Band 2 Gain", "-2.50 dB", 0.458),
        _param(27, "Band 2 Q", "1.000", 0.5),
        _param(28, "Band 2 Shape", "Bell", 0.0),
        _param(46, "Band 3 Used", "Unused", 0.0),
    ]
    return params


def test_eq_summary_lists_only_used_bands() -> None:
    summary = summarize(PluginDescriptor(name="VST: FabFilter Pro-Q 4 (FabFilter)",
                                         active=True, parameters=_pro_q()))
    assert summary["family"] == "eq"
    assert summary["used_bands"] == 2
    assert summary["bands"][0]["Shape"] == "Low Cut"
    assert summary["bands"][1]["Gain"] == "-2.50 dB"
    assert summary["active"] is True


def test_compressor_summary_keeps_named_fields() -> None:
    params = [
        _param(1, "Threshold", "-24.00 dB", 0.6),
        _param(2, "Ratio", "3.00:1", 0.4),
        _param(5, "Attack", "0.255 ms", 0.1),
        _param(6, "Release", "209.2 ms", 0.41),
        _param(34, "Mix", "100.0%", 0.5),
    ]
    summary = summarize(PluginDescriptor(name="VST: FabFilter Pro-C 2 (FabFilter)",
                                         active=True, parameters=params))
    assert summary["family"] == "compressor"
    assert summary["fields"]["Threshold"] == "-24.00 dB"
    assert summary["fields"]["Ratio"] == "3.00:1"


def test_reverb_summary_reports_mix_and_mode() -> None:
    params = [
        _param(0, "Mix", "28.0 %", 0.28),
        _param(1, "PreDelay", "25.00 ms", 0.05),
        _param(2, "Decay", "1.23 s", 0.285),
        _param(13, "HighCut", "7000 Hz", 0.85),
        _param(16, "ReverbMode", "Palace"),
        _param(15, "ColorMode", "eighties"),
    ]
    summary = summarize(PluginDescriptor(name="VST3: ValhallaVintageVerb (Valhalla DSP, LLC)",
                                         active=True, parameters=params))
    assert summary["family"] == "reverb"
    assert summary["fields"]["Mix"] == "28.0 %"
    assert summary["fields"]["ReverbMode"] == "Palace"
    assert summary["mapped"] is True


def test_unknown_plugin_is_declared_unmapped() -> None:
    summary = summarize(PluginDescriptor(name="VST: Mystery Unit",
                                         active=True,
                                         parameters=[_param(0, "Foo", "1.0")]))
    assert summary["mapped"] is False
    assert summary["reason"] == "no_parameter_rule_for_this_plugin"


def test_store_migrates_entries_to_a_new_project_id(tmp_path: Path) -> None:
    """save-as moves the project id; summaries must travel with the file."""
    store = PluginStateStore(tmp_path)
    summary = summarize(PluginDescriptor(name="VST: FabFilter Pro-C 2 (FabFilter)",
                                         active=True,
                                         parameters=[_param(1, "Threshold", "-24.00 dB")]))
    chain = chain_fingerprint("<FXCHAIN same>")
    store.put(PID, TRACK, FX, summary=summary, chain_fingerprint=chain)

    moved = store.migrate(PID, "reaper:{M}:C:/x/other.rpp")
    assert moved == 1
    carried, state = store.status("reaper:{M}:C:/x/other.rpp", TRACK, FX, chain)
    assert state == "current" and carried is not None
    assert store.status(PID, TRACK, FX, chain)[1] == "current"


def test_store_reuses_a_summary_only_for_the_same_chain(tmp_path: Path) -> None:
    store = PluginStateStore(tmp_path)
    summary = summarize(PluginDescriptor(name="VST: FabFilter Pro-C 2 (FabFilter)",
                                         active=True,
                                         parameters=[_param(1, "Threshold", "-24.00 dB")]))
    chain = chain_fingerprint("<FXCHAIN one>")
    store.put(PID, TRACK, FX, summary=summary, chain_fingerprint=chain)

    current, state = store.status(PID, TRACK, FX, chain)
    assert state == "current" and current and current["fields"]["Threshold"] == "-24.00 dB"

    stale, state = store.status(PID, TRACK, FX, chain_fingerprint("<FXCHAIN two>"))
    assert stale is None and state == "stale"

    unverified, state = store.status(PID, TRACK, FX, None)
    assert state == "unverified" and unverified is not None
