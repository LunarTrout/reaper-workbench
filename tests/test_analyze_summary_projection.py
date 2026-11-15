"""Focused tests for the lossless ``reaper_analyze`` summary projection.

The fixture is shaped like the response measured in HB-059: 34 sources, 24
metrics with identical descriptors, one 1.5 KB ``unverified`` dependency list
repeated per source and six identical project/master component hashes.  The
tests prove that the projection keeps every fact exactly once or linked, that
the pre-normalized rows reconstruct byte-for-byte, and how much smaller the
payload becomes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time

from reaper_audio import mcp_server
from reaper_audio.context.analyze_summary import (
    COMPONENT_KEYS_KEY,
    COMPONENT_TABLE_KEY,
    LOCAL_METRIC_DEFINITIONS_KEY,
    METRIC_REGISTRY_KEY,
    PROJECTION_KEY,
    SHARED_CAPTURE_KEY,
    SOURCE_ID_KEY,
    SUMMARY_PROJECTION_VERSION,
    VERBATIM_METRICS_KEY,
    assert_projection_is_lossless,
    measure_projection_sizes,
    project_analyze_summary,
    reconstruct_sources,
)

METRIC_DEFINITIONS: dict[str, tuple[str, str | None, float | None]] = {
    "peak_dbfs": ("dBFS", None, None),
    "true_peak_estimate_dbfs": ("dBTP", "4x polyphase FIR estimate; not validated for standards compliance", None),
    "rms_dbfs": ("dBFS", None, None),
    "crest_db": ("dB", None, None),
    "integrated_lufs": ("LUFS", None, None),
    "loudness_range_lu": ("LU", "not computed: LRA requires 3 s analysis windows", None),
    "clipped_sample_ratio": ("ratio", None, None),
    "spectral_centroid_hz": ("Hz", None, None),
    "transient_density_per_s": ("1/s", None, 0.8),
    "transient_count": ("count", None, 0.8),
    "stereo_correlation": ("correlation", None, None),
    "side_mid_db": ("dB", None, None),
    "mono_fold_loss_db": ("dB", None, None),
    "modulation_rate_hz": ("Hz", "spectral modulation peak; interpretation depends on source role", 0.5),
    "modulation_depth": ("ratio", None, 0.5),
    "pitch_jitter_pct": ("percent", None, 0.4),
    "coarse_speech_band_ratio": ("ratio", None, None),
    "dc_offset": ("ratio", None, None),
    "clipped_sample_count": ("count", None, None),
    "two_ms_event_count": ("count", None, 0.9),
    "onset_count": ("count", None, 0.7),
    "spike_count": ("count", None, 0.7),
    "clipping_event_count": ("count", None, None),
    "local_tempo_median_bpm": ("BPM", None, 0.3),
}

SHARED_COMPONENTS = {
    "master:automation": "8fe2a494cc33aea0216985cc3cc2fa3ae36a254a4d8e5998eebdce00ddf73cf9",
    "master:fx": "3a92ecacf47ec062dcec29f18a38b0b2e07d208087366fba6c9681e2de829746",
    "master:fx_state": "fdf8e2dce98d35267abf9fb47fef9322b9ca448cfac5e5b54b2a82ccd69396a6",
    "master:track_state": "b2b47040de66fc8d36fb060e37e4a69fe8081b609a52c611a8aadd9baa43476b",
    "project": "79e610b52052fc963c11bdd137b3f2a1557d4782bb547de0a86b27670a6dab35",
    "tempo": "c92046f647ef0e479afba95bc9c7414557ca86eaeac948cea7969cda024546c3",
}

UNVERIFIED = [
    {
        "class": "media_file_content",
        "policy": "cross_capture_comparison_unknown",
        "reason": ("the digest proves source paths and item state, not the bytes of the media "
                   "files on disk; a file replaced under the same path is not detected by a "
                   "project-state digest"),
        "scope": "all_project_audio",
    },
    {
        "class": "external_realtime_input",
        "policy": "cross_capture_comparison_unknown",
        "reason": ("live inputs, hardware monitoring and external clock are outside the "
                   "project-state digest"),
        "scope": "all_project_audio",
    },
    {
        "class": "project_default_pan_law",
        "policy": "cross_capture_comparison_unknown",
        "reason": ("per-track D_PANLAW is read, but a value of -1 means \"project default\" and "
                   "REAPER exposes no documented GetSetProjectInfo key for the project pan law"),
        "scope": "all_project_audio",
    },
    {
        "class": "plugin_binary_version",
        "policy": "cross_capture_comparison_unknown",
        "reason": ("FX identity, bypass, parameter values and serialized chain state are hashed, "
                   "but REAPER exposes no version/model string for the loaded plugin binary"),
        "scope": "all_project_audio",
    },
    {
        "class": "unexposed_track_routing_flags",
        "policy": "cross_capture_comparison_unknown",
        "reason": ("FX pin mappings, per-track C_BEATATTACHMODE timebase and fixed-lane "
                   "C_LANEPLAYS/C_ALLLANESPLAY playback flags are not read as numeric digest "
                   "fields"),
        "scope": "all_project_audio",
    },
]

REVISION = {
    "change_count": 6753,
    "epoch": 10,
    "epoch_basis": "bridge_session_project_generation",
    "instance_id": "6af87df4-3845e4b87a",
    "path": "C:/projects/example/versions/mix_v8.RPP",
    "project_id": "reaper:{A15E3431}:C:/projects/example/versions/mix_v8.RPP",
}


def _guid(index: int) -> str:
    return f"{{{index:08X}-1111-2222-3333-{index:012X}}}"


def _synthetic_result(count: int = 34) -> dict:
    """One analyze result shaped like the measured 34-source payload."""
    guids = [_guid(index) for index in range(count)]
    own_components = {
        guid: {f"track:{guid}:self:{kind}": f"{index:02X}" * 32
               for index, kind in enumerate(
                   ("automation", "fx", "fx_state", "items", "routing", "track_state"))}
        for guid in guids
    }
    sources = []
    for index, guid in enumerate(guids):
        name = "Sample Drums" if index == 0 else f"Track {index:02d}"
        components = dict(SHARED_COMPONENTS)
        components.update(own_components[guid])
        if index == 0:
            # The master mix depends on every track, so its own manifest carries
            # every track's component hashes as well.
            for other in guids:
                components.update(own_components[other])
        metrics = {}
        for metric, (unit, note, confidence) in METRIC_DEFINITIONS.items():
            if metric == "loudness_range_lu":
                value = None
            elif unit in {"count"}:
                value = float(index * 3)
            else:
                value = round(-1.0 - index * 0.125, 12)
            metrics[metric] = {"value": value, "unit": unit,
                               "confidence": confidence, "note": note}
        sources.append({
            "source_guid": guid,
            "name": name,
            "is_master": index == 0,
            "volume": round(1.0 - index * 0.01, 6),
            "pan": 0.0,
            "mute": False,
            "capture": {
                "asset_id": f"ast_sha256_{guid.strip('{}').replace('-', '')}",
                "source_guid": guid,
                "name": name,
                "is_master": index == 0,
                "stage": ("master_mix_post_fx" if index == 0 else
                          "selected_track_via_master_parallel_batch_post_fx"),
                "capture_method": "native_render",
                "sample_rate": 48_000,
                "channels": 2,
                "frames": 2_414_400,
                "duration_s": 50.3,
                "bytes": 14_487_090,
                "sha256": f"{index:02x}" * 32,
                "signal_window": {"file_end_s": 50.3, "file_start_s": 0.0,
                                  "project_end_s": 200.0, "project_start_s": 150.0},
                "capture_revision": copy.deepcopy(REVISION),
                "render_key": f"rk1:{index:02x}" + "ab" * 31,
                "render_key_complete": True,
                "render_dependencies": {
                    "components_sha256": components,
                    "dependency_guids": list(guids) if index == 0 else [guid],
                    "manifest_schema": "reaper-workbench.render-manifest/1",
                    "unknown": [],
                    "unverified": copy.deepcopy(UNVERIFIED),
                },
            },
            "metrics": metrics,
            "audio_uri": f"reaper://analyses/a-synthetic/audio/{guid}",
        })
    return {
        "ok": True,
        "analysis_id": "a-synthetic",
        "source_count_including_master": count,
        "sources": sources,
        "coverage": {"sources_analyzed": count, "references_analyzed": 0},
        "analysis_windows": {"measured": {"start_s": 150.0, "end_s": 151.0}},
    }


def _payload_chars(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def own_component_names(guid: str) -> tuple[str, ...]:
    kinds = ("automation", "fx", "fx_state", "items", "routing", "track_state")
    return tuple(f"track:{guid}:self:{kind}" for kind in kinds)


def test_projection_reconstructs_all_34_sources_exactly() -> None:
    result = _synthetic_result()
    originals = copy.deepcopy(result["sources"])
    projected = project_analyze_summary(result)

    meta = projected[PROJECTION_KEY]
    assert meta["schema_version"] == SUMMARY_PROJECTION_VERSION
    assert meta["applied"] is True
    assert meta["source_count"] == 34
    assert meta["metric_count"] == len(METRIC_DEFINITIONS) == 24

    # Every source row survives with its identity and its audio link.
    assert len(projected["sources"]) == 34
    for row, original in zip(projected["sources"], originals):
        assert row[SOURCE_ID_KEY] == original["source_guid"]
        assert row["source_guid"] == original["source_guid"]
        assert row["name"] == original["name"]
        assert row["is_master"] == original["is_master"]
        assert row["audio_uri"] == original["audio_uri"]
        assert row["volume"] == original["volume"]
        assert row["pan"] == original["pan"]
        assert row["mute"] == original["mute"]
        # All 24 named values stay on the row, including nulls.
        assert list(row["metrics"]) == list(original["metrics"])
        assert row["metrics"]["loudness_range_lu"] is None

    # Metric definitions live once and are exactly the measured ones.
    registry = projected[METRIC_REGISTRY_KEY]
    assert set(registry) == set(METRIC_DEFINITIONS)
    for name, (unit, note, confidence) in METRIC_DEFINITIONS.items():
        assert registry[name] == {"unit": unit, "confidence": confidence, "note": note}

    # Shared capture facts and the dependency manifest are stored once.
    shared = projected[SHARED_CAPTURE_KEY]
    assert shared["capture_method"] == "native_render"
    assert shared["signal_window"]["project_start_s"] == 150.0
    assert shared["capture_revision"] == REVISION
    assert shared["render_dependencies"]["manifest_schema"] == \
        "reaper-workbench.render-manifest/1"
    assert shared["render_dependencies"]["unknown"] == []
    assert shared["render_dependencies"]["unverified"] == UNVERIFIED
    table = projected[COMPONENT_TABLE_KEY]
    for name, value in SHARED_COMPONENTS.items():
        assert table[name] == value
        assert name in shared["render_dependencies"]["component_keys"]
    for row in projected["sources"]:
        assert "signal_window" not in row["capture"]
        assert "capture_revision" not in row["capture"]
        dependencies = row["capture"]["render_dependencies"]
        assert "unverified" not in dependencies
        assert "manifest_schema" not in dependencies
        assert "dependency_guids" in dependencies
        assert not set(dependencies.get("components_sha256") or {}) & set(table)
        assert not (set(dependencies.get("component_keys") or [])
                    & set(shared["render_dependencies"]["component_keys"]))

    # The master repeats every other track's component hashes; each is stored once
    # in the table and the master links to it. Its own single-use hashes stay local.
    master = projected["sources"][0]
    master_dependencies = master["capture"]["render_dependencies"]
    master_own = set(own_component_names(originals[0]["source_guid"]))
    assert set(master_dependencies.get("components_sha256") or {}) == master_own
    assert len(master_dependencies["component_keys"]) == (34 - 1) * 6
    assert len(master_dependencies["dependency_guids"]) == 34
    third = projected["sources"][3]
    third_dependencies = third["capture"]["render_dependencies"]
    assert third_dependencies.get("components_sha256") is None
    assert set(third_dependencies["component_keys"]) == set(
        f"track:{originals[3]['source_guid']}:self:{kind}"
        for kind in ("automation", "fx", "fx_state", "items", "routing", "track_state"))

    # Exactness: the rows reconstruct with every fact, once.
    assert reconstruct_sources(projected) == originals
    inventory = assert_projection_is_lossless(originals, projected)
    assert inventory["reconstructed_rows"] == 34
    assert inventory["shared_component_hashes"] == len(SHARED_COMPONENTS)
    assert inventory["component_hash_table"] == len(SHARED_COMPONENTS) + (34 - 1) * 6
    assert inventory["metric_registry_metrics"] == 24
    assert inventory["duplicate_facts"] == 0

    # The input payload was not mutated by the projection.
    assert result["sources"] == originals


def test_metric_descriptor_override_is_kept_losslessly() -> None:
    result = _synthetic_result()
    result["sources"][7]["metrics"]["rms_dbfs"]["note"] = "measured on a shortened window"
    originals = copy.deepcopy(result["sources"])

    projected = project_analyze_summary(result)

    override = projected["sources"][7][LOCAL_METRIC_DEFINITIONS_KEY]["rms_dbfs"]
    assert override["note"] == "measured on a shortened window"
    assert projected[METRIC_REGISTRY_KEY]["rms_dbfs"]["note"] is None
    assert LOCAL_METRIC_DEFINITIONS_KEY not in projected["sources"][6]
    assert reconstruct_sources(projected) == originals
    assert_projection_is_lossless(originals, projected)


def test_non_mapping_metric_object_is_kept_verbatim() -> None:
    result = _synthetic_result()
    result["sources"][2]["metrics"]["onset_count"] = 12.0
    originals = copy.deepcopy(result["sources"])

    projected = project_analyze_summary(result)

    assert projected["sources"][2][VERBATIM_METRICS_KEY] == ["onset_count"]
    assert projected["sources"][2]["metrics"]["onset_count"] == 12.0
    assert reconstruct_sources(projected) == originals


def test_unexpected_row_shape_falls_back_without_losing_data() -> None:
    result = {"ok": True, "sources": ["not-a-row"]}
    projected = project_analyze_summary(result)

    assert projected[PROJECTION_KEY]["applied"] is False
    assert projected["sources"] == result["sources"]
    assert reconstruct_sources(projected) == result["sources"]


def test_empty_source_list_is_reported_not_invented() -> None:
    projected = project_analyze_summary({"ok": True, "sources": []})

    assert projected[PROJECTION_KEY]["applied"] is False
    assert projected["sources"] == []


def test_summary_projection_reduces_the_hb059_shaped_payload() -> None:
    result = _synthetic_result()
    projected = project_analyze_summary(result)
    sizes = measure_projection_sizes(result, projected)

    print(json.dumps(sizes, indent=2))
    assert sizes["before_chars"] > 150_000
    assert sizes["after_chars"] < sizes["before_chars"]
    # The shared 1.5 KB dependency list, six component hashes and 24 metric
    # descriptors were repeated 34 times.  The measured real payload drops from
    # 253,402 to 110,876 compact chars (350,862 -> 136,516 pretty), ratio 0.44;
    # the residual is dominated by the master row's cross-track component
    # copies, which this projection deliberately leaves on their source.
    assert sizes["after_ratio"] < 0.5
    assert sizes["saved_chars"] == sizes["before_chars"] - sizes["after_chars"]


def test_only_summary_detail_is_projected_and_full_stays_complete() -> None:
    result = _synthetic_result()
    original = copy.deepcopy(result)

    summary = mcp_server._summarize_analyze_result(copy.deepcopy(result), "summary")
    full = mcp_server._summarize_analyze_result(copy.deepcopy(result), "full")

    assert PROJECTION_KEY in summary
    assert PROJECTION_KEY not in full
    assert full == original
    # detail="full" keeps the verbose per-source rows untouched.
    assert full["sources"][0]["capture"]["render_dependencies"]["unverified"] == UNVERIFIED
    assert "signal_window" in full["sources"][0]["capture"]
    # The measured payload itself is never mutated by either call.
    assert result == original


def _uri_tail(rows: list[dict]) -> list[dict]:
    """Drop the analysis-id prefix from audio links so two calls can be compared."""
    return [{**row, "audio_uri": (row.get("audio_uri") or "").rsplit("/", 1)[-1]}
            for row in rows]


def test_summary_and_full_archive_analyze_describe_the_same_rows(
        tmp_path, monkeypatch) -> None:
    """The projected summary must reconstruct exactly the verbose rows of the tool."""
    from test_capture_panorama import _record

    from reaper_audio.context.full_capture import CaptureArchive
    from reaper_audio.context.lifecycle import PanoramaLifecycle

    base, files = _record(tmp_path, capture_id=f"cap_summary_{int(time.time() * 1000)}")
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(base, files)
    lifecycle = PanoramaLifecycle(str(archive.root), page_bytes=512)
    opened = lifecycle.open(base.project_id, capture_id=base.capture_id)
    hashes = [opened["page_hash"]]
    for index in range(1, opened["page_count"]):
        hashes.append(lifecycle.page(opened["handle"], opened["revision"], index)["page_hash"])
    receipt = lifecycle.ack(opened["handle"], opened["revision"], hashes)["receipt"]

    def no_reaper(*args, **kwargs):
        raise AssertionError("archived analysis must not contact REAPER")

    monkeypatch.setattr(mcp_server, "_session", no_reaper)
    monkeypatch.setattr(mcp_server, "_focus_reaper_window", no_reaper)

    common = {"capture_id": base.capture_id, "panorama_receipt": receipt,
              "archive_root": str(archive.root)}
    summary = mcp_server.reaper_analyze(**common)
    full = mcp_server.reaper_analyze(**common, detail="full")

    assert summary["ok"] is True, summary
    meta = summary[PROJECTION_KEY]
    assert meta["applied"] is True
    assert meta["source_count"] == summary["source_count_including_master"]
    assert meta["metric_count"] == len(summary[METRIC_REGISTRY_KEY])
    assert summary[METRIC_REGISTRY_KEY]

    restored = reconstruct_sources(summary)
    assert _uri_tail(restored) == _uri_tail(full["sources"])
    for row in summary["sources"]:
        assert row["audio_uri"].startswith(
            f"reaper://analyses/{summary['analysis_id']}/audio/")

    # The durable record keeps the verbose rows; the projection is model-facing.
    record = mcp_server._analysis_record(summary["analysis_id"], str(archive.root))
    assert isinstance(record["report"]["sources"], dict)
    stored_row = record["report"]["sources"][base.audio[0]["source_guid"]]
    assert "capture" in stored_row and "metrics" in stored_row


# ---------------------------------------------------------------------------
# Shape independence: any source count, metric set and dependency map
# ---------------------------------------------------------------------------

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _metric(value, *, unit: str = "dB", note: str | None = None,
            confidence: float | None = None) -> dict:
    return {"value": value, "unit": unit, "confidence": confidence, "note": note}


def _capture(guid: str, components: dict[str, str], *,
             guids: list[str] | None = None) -> dict:
    return {
        "asset_id": f"ast_{guid}",
        "source_guid": guid,
        "name": guid,
        "is_master": False,
        "sha256": _hash(guid),
        "render_dependencies": {
            "components_sha256": dict(components),
            "dependency_guids": list(guids or [guid]),
            "manifest_schema": "reaper-workbench.render-manifest/1",
            "unknown": [],
            "unverified": copy.deepcopy(UNVERIFIED),
        },
    }


def _row(guid: str, *, metrics: dict | None = None, capture: dict | None = None) -> dict:
    return {
        "source_guid": guid,
        "name": guid,
        "is_master": False,
        "volume": 1.0,
        "pan": 0.0,
        "mute": False,
        "capture": _capture(guid, {}) if capture is None else capture,
        "metrics": {} if metrics is None else metrics,
        "audio_uri": f"reaper://analyses/a-synthetic/audio/{guid}",
    }


def test_single_source_project_round_trips() -> None:
    rows = [_row("{ONE}", metrics={"peak_dbfs": _metric(-3.0)},
                 capture={**_capture("{ONE}", {"master:fx": _hash("fx")}),
                          "is_master": True})]

    projected = project_analyze_summary({"sources": rows})

    # One source repeats nothing: the projection cannot pay for its shared blocks,
    # so the plain complete rows are returned instead of a larger payload.
    assert PROJECTION_KEY not in projected
    assert projected["sources"] == rows
    assert reconstruct_sources(projected) == rows
    assert assert_projection_is_lossless(rows, projected)["reconstructed_rows"] == 1


def test_two_source_project_shares_repeated_component_hashes() -> None:
    project_hash = _hash("project")
    rows = [
        _row("{A}", capture=_capture("{A}", {"project": project_hash,
                                             f"track:{{A}}:fx": _hash("a")})),
        _row("{B}", capture=_capture("{B}", {"project": project_hash,
                                             f"track:{{B}}:fx": _hash("b")})),
    ]

    projected = project_analyze_summary({"sources": rows})

    # Exact either way; when the layout is applied the repeated hash is stored once.
    assert reconstruct_sources(projected) == rows
    assert_projection_is_lossless(rows, projected)
    if PROJECTION_KEY in projected:
        assert json.dumps(projected, ensure_ascii=False).count(project_hash) == 1
        shared_dependencies = projected["shared_capture"]["render_dependencies"]
        assert ("component_keys" in shared_dependencies) != (
            "components_sha256" in shared_dependencies)
        for row in projected["sources"]:
            dependencies = row["capture"]["render_dependencies"]
            assert "project" not in (dependencies.get("components_sha256") or {})
            assert "project" not in dependencies.get("component_keys", [])


def test_arbitrary_metric_names_and_counts() -> None:
    rows = [
        _row("{A}", metrics={"peak_dbfs": _metric(-1.0),
                             "custom one": _metric(2, unit="x")}),
        _row("{B}", metrics={"peak_dbfs": _metric(-2.0),
                             "odd/name:2": _metric(None, unit="ratio",
                                                   note="not computed"),
                             "another": _metric(0.5),
                             "peak_dbfs_2": _metric(9)}),
        _row("{C}", metrics={}),
    ]

    projected = project_analyze_summary({"sources": rows})

    # Arbitrary names and per-source subsets survive exactly, whatever the layout.
    assert reconstruct_sources(projected) == rows
    assert_projection_is_lossless(rows, projected)


def test_missing_optional_fields_round_trip() -> None:
    rows = [
        _row("{A}", capture={"asset_id": "a", "source_guid": "{A}", "name": "A",
                             "is_master": False}),
        _row("{B}", capture={"asset_id": "b", "source_guid": "{B}", "name": "B",
                             "is_master": False,
                             "render_dependencies": {"dependency_guids": ["{B}"]}}),
        _row("{C}", capture={"asset_id": "c", "source_guid": "{C}", "name": "C",
                             "is_master": True,
                             "render_dependencies": {
                                 "components_sha256": {"master:fx": _hash("c")}}}),
    ]

    projected = project_analyze_summary({"sources": rows})

    assert reconstruct_sources(projected) == rows
    assert_projection_is_lossless(rows, projected)


def test_differing_descriptors_are_kept_per_source() -> None:
    rows = [
        _row("{A}", metrics={"m": _metric(1.0, note="first")}),
        _row("{B}", metrics={"m": _metric(2.0, note="second")}),
        _row("{C}", metrics={"m": _metric(3.0, note="third", confidence=0.5)}),
    ]

    projected = project_analyze_summary({"sources": rows})

    assert reconstruct_sources(projected) == rows
    assert_projection_is_lossless(rows, projected)
    if PROJECTION_KEY in projected:
        assert projected[METRIC_REGISTRY_KEY]["m"] == {
            "unit": "dB", "confidence": None, "note": "first"}
        assert LOCAL_METRIC_DEFINITIONS_KEY not in projected["sources"][0]
        assert projected["sources"][1][LOCAL_METRIC_DEFINITIONS_KEY]["m"][
            "note"] == "second"


def test_conflicting_component_values_are_not_unified() -> None:
    rows = [
        _row("{A}", capture=_capture("{A}", {"shared:bus": _hash("a")})),
        _row("{B}", capture=_capture("{B}", {"shared:bus": _hash("b")})),
    ]

    projected = project_analyze_summary({"sources": rows})

    assert COMPONENT_TABLE_KEY not in projected
    for row, expected in zip(projected["sources"], (rows[0], rows[1])):
        assert row["capture"]["render_dependencies"]["components_sha256"] == \
            expected["capture"]["render_dependencies"]["components_sha256"]
    assert reconstruct_sources(projected) == rows


def test_master_cross_track_hashes_are_stored_once() -> None:
    tracks = [f"{{T{index}}}" for index in range(4)]
    kinds = ("automation", "fx", "items")
    own = {guid: {f"track:{guid}:{kind}": _hash(f"{guid}:{kind}") for kind in kinds}
           for guid in tracks}
    master_components = {"project": _hash("project"), "tempo": _hash("tempo")}
    for guid in tracks:
        master_components.update(own[guid])
    rows = [_row("{M}", capture={**_capture("{M}", master_components, guids=tracks),
                                 "is_master": True})]
    rows += [_row(guid, capture=_capture(guid, own[guid])) for guid in tracks]

    result = {"sources": rows}
    projected = project_analyze_summary(result)

    table = projected[COMPONENT_TABLE_KEY]
    assert set(table) == {name for guid in tracks for name in own[guid]}
    master = projected["sources"][0]["capture"]["render_dependencies"]
    assert master.get("components_sha256") == {"project": _hash("project"),
                                               "tempo": _hash("tempo")}
    assert sorted(master["component_keys"]) == sorted(
        name for guid in tracks for name in own[guid])
    for row, guid in zip(projected["sources"][1:], tracks):
        dependencies = row["capture"]["render_dependencies"]
        assert dependencies.get("components_sha256") in (None, {})
        assert sorted(dependencies["component_keys"]) == sorted(own[guid])

    assert reconstruct_sources(projected) == rows
    inventory = assert_projection_is_lossless(rows, projected)
    assert inventory["component_hash_table"] == len(table) == len(tracks) * len(kinds)
    sizes = measure_projection_sizes(result, projected)
    print(json.dumps(sizes, indent=2))
    assert sizes["after_chars"] < sizes["before_chars"]
