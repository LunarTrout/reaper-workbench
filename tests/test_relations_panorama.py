"""Integration acceptance: one capture panorama carries the relation core."""

from __future__ import annotations

from reaper_audio.context.lifecycle import stable_fact_view
from reaper_audio.context.panorama import build_panorama


def _with_rms(record) -> dict:
    mapping = dict(record.to_mapping())
    analysis = {}
    for guid, report in mapping["analysis"].items():
        report = dict(report)
        metrics = dict(report.get("metrics") or {})
        metrics["rms_dbfs"] = {
            "value": -18.0,
            "unit": "dBFS",
            "confidence": None,
            "note": "injected by the relation integration test",
        }
        report["metrics"] = metrics
        analysis[guid] = report
    mapping["analysis"] = analysis
    return mapping


def test_panorama_carries_canonical_relations_and_completeness(tmp_path) -> None:
    from test_capture_panorama import _record

    record, _paths = _record(tmp_path)
    document = build_panorama(_with_rms(record))
    relations = document["relations"]

    assert relations["schema_version"] == 2
    assert relations["canonical_schema_version"] == 1
    assert relations["binding"]["capture_id"]
    assert relations["binding"]["stage"] == "post_fx"
    assert relations["binding"]["window"]["start_s"] < relations["binding"]["window"]["end_s"]

    coverage = relations["coverage"]
    assert coverage["band_dominance"]["expected"] == 1
    assert coverage["band_dominance"]["evaluated"] == 1
    assert coverage["band_dominance"]["status"] == "complete"
    assert coverage["track_master_level_relation"]["expected"] == 2
    assert coverage["track_master_level_relation"]["evaluated"] == 2
    assert coverage["track_master_level_relation"]["status"] == "complete"

    pairs = relations["evaluated_pairs"]
    assert len(pairs) == 1
    row = pairs[0]
    assert row["id"].startswith("rel_")
    assert row["result"] in {"evaluated", "no_dominant_band"}
    assert row["from"].startswith("track:") and row["to"].startswith("track:")
    assert "dep_set" not in row

    kinds = relations["kinds"]
    assert set(kinds) == {"track_master_level_relation"}
    block = kinds["track_master_level_relation"]
    # Shared meaning, units, caveats and alternatives appear once per kind.
    assert block["method"] == "analysis.relations.track_master_level_relation"
    assert block["units"]["share_of_summed_power_pct"] == "%"
    assert block["uncertainty"]["status"] == "unknown"
    assert len(block["alternatives"]) >= 2
    assert "a fader" in block["is_not"]

    edges = relations["edges"]
    assert edges and all(edge["kind"] == "track_master_level_relation" for edge in edges)
    for edge in edges:
        assert edge["id"].startswith("rel_")
        assert "share_of_summed_power_pct" in edge["metrics"]
        assert set(edge["metrics"]) <= set(block["units"])
        assert edge["coverage"]["status"] == "complete"
        assert "interpretation" not in edge
        assert "provenance" not in edge

    # Set-level provenance and asset identity live once, not per row.
    assert relations["sources"]
    assert all("asset_sha256" in item for item in relations["sources"].values())

    assert relations["reference_status"] == "not_supplied"
    assert relations["page_contract"]["canonical"] is True
    assert relations["page_contract"]["top_k_is_not_complete"] is True
    assert relations["completeness"]["pairs_evaluated"] == 1
    assert relations["completeness"]["edges_omitted_by_display_cap"] == 0
    assert document["domain_counts"]["relations"] == len(edges)
    assert document["domain_counts"]["relation_evaluated_pairs"] >= 1


def test_panorama_relation_ids_are_stable_across_rebuilds(tmp_path) -> None:
    from test_capture_panorama import _record

    record, _paths = _record(tmp_path)
    first = build_panorama(_with_rms(record))["relations"]
    second = build_panorama(_with_rms(record))["relations"]
    assert first["content_hash"] == second["content_hash"]
    assert [item["id"] for item in first["edges"]] == [
        item["id"] for item in second["edges"]
    ]
    assert [item["id"] for item in first["evaluated_pairs"]] == [
        item["id"] for item in second["evaluated_pairs"]
    ]
    assert first["evaluated_pairs"][0]["id"].startswith("rel_")


def test_relation_fact_view_drops_capture_local_addresses(tmp_path) -> None:
    """The fact view keeps measurements and drops detail addresses and row ids."""
    from test_capture_panorama import _record

    record, _paths = _record(tmp_path, capture_id="cap_relations_fact_a")
    document = build_panorama(_with_rms(record))
    view = stable_fact_view(document)
    # Linked payloads keep only their content identity in the fact view.
    assert view["details"]["entries"]
    assert all(set(entry) == {"ref", "kind", "count", "truncated", "content_sha256"}
               for entry in view["details"]["entries"])
    assert view["relations"]["edges"]
    assert all("id" not in edge for edge in view["relations"]["edges"])
    assert all("id" not in pair for pair in view["relations"]["evaluated_pairs"])
    assert all("asset_sha256" not in row
               for row in view["relations"]["sources"].values())
    # The measurement values themselves survive the projection.
    original = document["relations"]["edges"][0]["metrics"]
    assert view["relations"]["edges"][0]["metrics"] == original
