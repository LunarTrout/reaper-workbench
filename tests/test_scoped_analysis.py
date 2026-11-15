"""Scoped, batched analysis contract and discoverable analyzer catalog."""

from __future__ import annotations

from reaper_audio import mcp_server


def _full(interval):
    return {
        "ok": True,
        "analysis_id": f"a-{interval['start_s']}-{interval['end_s']}",
        "evidence_revision": {"project_id": "reaper:test", "change_count": 7,
                              "path": "C:/song.rpp", "instance_id": "i", "epoch": 1},
        "initial_context_revision": {"project_id": "reaper:test", "change_count": 7},
        "project": {"project_id": "reaper:test"},
        "capture_id": "cap_1",
        "sources": [
            {"source_guid": "g1", "source_id": "track:g1", "name": "Guitar",
             "metrics": {"peak_dbfs": {"value": -3.0, "unit": "dBFS"}}},
            {"source_guid": "g2", "source_id": "track:g2", "name": "Bass",
             "metrics": {"peak_dbfs": {"value": -6.0, "unit": "dBFS"}}},
        ],
        "coverage": {"sources_analyzed": 2},
        "report": {
            "sources": {
                "g1": {"curves": {"temporal_profile": {"times": [0.0], "cells": []}}},
                "g2": {"curves": {"temporal_profile": {"times": [0.0], "cells": []}}},
            },
            "masking_evidence": {"pairs": [], "status": "available"},
            "cross_source_band_timeline": {"cells": [], "coverage": {"status": "available"}},
            "pairwise_phase": [],
            "partial_loudness": None,
        },
        "report_uri": f"reaper://analyses/a-{interval['start_s']}-{interval['end_s']}/report",
    }


def test_catalog_exposes_stable_ids_scope_and_relative_cost() -> None:
    catalog = mcp_server.ANALYZER_CATALOG
    assert {"metrics", "temporal_profile", "masking", "phase",
            "partial_loudness", "mix_evidence"} <= set(catalog)
    for name, row in catalog.items():
        assert row["description"] and row["scope"] and row["prerequisites"]
        assert row["cost"]["class"] in {"light", "medium", "heavy"}
        assert "not measured" in row["cost"]["basis"] or "relative" in row["cost"]["basis"]
    capabilities = mcp_server.reaper_capabilities(mode="tools")
    assert set(capabilities["analyzers"]) == set(catalog)
    assert "reaper_analyze" in capabilities["analyzer_rule"]


def test_one_scoped_batch_runs_several_analyzers_for_named_sources(
        tmp_path, monkeypatch) -> None:
    calls = []

    def fake(capture_id, start_s, end_s, **_kwargs):
        calls.append((capture_id, start_s, end_s))
        return _full({"start_s": start_s, "end_s": end_s})

    monkeypatch.setattr(mcp_server, "_analyze_archived_capture", fake)
    result = mcp_server.reaper_analyze(
        capture_id="cap_1", archive_root=str(tmp_path),
        analyzers=["metrics", "mix_evidence", "temporal_profile"],
        source_ids=["track:g1"],
        intervals=[{"start_s": 1.0, "end_s": 2.0}, {"start_s": 5.0, "end_s": 6.0}])

    assert result["status"] == "scoped_analysis"
    assert result["scope"]["kind"] == "scoped"
    assert result["scope"]["whole_project"] is False
    assert result["scope"]["source_count"] == 2
    assert "not an automatic next step" in result["next_step"]
    assert len(calls) == len(result["runs"]) == 2
    for run in result["runs"]:
        assert run["coverage"]["returned_sources"] == ["g1"]
        assert run["coverage"]["scoped"] is True
        assert set(run["analyzers"]) == {"metrics", "mix_evidence", "temporal_profile"}
        assert run["analyzers"]["metrics"]["status"] == "available"
        assert len(run["analyzers"]["metrics"]["sources"]) == 1
        assert run["analyzers"]["mix_evidence"]["status"] == "available"
        assert run["analyzers"]["temporal_profile"]["status"] == "available"


def test_unknown_analyzer_and_source_fail_with_catalog_context(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "_analyze_archived_capture",
                        lambda capture_id, start_s, end_s, **kwargs: _full(
                            {"start_s": start_s, "end_s": end_s}))
    result = mcp_server.reaper_analyze(
        capture_id="cap_1", archive_root=str(tmp_path),
        analyzers=["not-an-analyzer"],
        intervals=[{"start_s": 0.0, "end_s": 1.0}])
    assert result["ok"] is False
    assert "analyzer" in result["error"]["message"].lower() or \
        "analyzer" in str(result["error"]).lower()


def _freeze(value):
    from types import MappingProxyType
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def test_scoped_batch_survives_a_frozen_archived_capture(tmp_path, monkeypatch) -> None:
    """An archived capture is served frozen; the scoped result must be JSON."""
    import json

    monkeypatch.setattr(
        mcp_server, "_analyze_archived_capture",
        lambda capture_id, start_s, end_s, **kwargs: _freeze(
            _full({"start_s": start_s, "end_s": end_s})))
    result = mcp_server.reaper_analyze(
        capture_id="cap_1", archive_root=str(tmp_path),
        analyzers=["metrics", "temporal_profile"],
        intervals=[{"start_s": 1.0, "end_s": 2.0}])

    assert result["status"] == "scoped_analysis"
    assert result["runs"][0]["coverage"]["scoped"] is True
    json.dumps(result, allow_nan=False)
