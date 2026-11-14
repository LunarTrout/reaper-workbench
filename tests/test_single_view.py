"""Contract tests for the single-response model view.

The normal path is one call: ``reaper_panorama_open`` returns the whole project
view plus a receipt and a context token, and every mutation keeps using that
receipt.  There are no mandatory pages, no cursor and no client reassembly.
Exact high-frequency rows come from one generic semantic query.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.lifecycle import PanoramaLifecycle
from reaper_audio.context.panorama import build_panorama
from reaper_audio.context.query import run_query
from reaper_audio.context.panorama_view import (
    DEFAULT_RESPONSE_BYTES,
    SECTION_INVENTORY,
    build_view,
    canonical,
    view_bytes,
)
from reaper_audio.errors import StaleContext, ValidationError

from test_capture_panorama import MASTER, TAKE_A, TRACK_A, _record


def _open(tmp_path: Path, **kwargs):
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    lifecycle = PanoramaLifecycle(archive.root)
    return record, archive, lifecycle, lifecycle.single(record.project_id, **kwargs)


def test_open_delivers_the_whole_view_in_one_result(tmp_path: Path) -> None:
    _record_obj, _archive, lifecycle, opened = _open(tmp_path)

    assert opened["status"] == "complete"
    assert opened["delivery"]["mode"] == "single_response"
    assert opened["delivery"]["pages"] is False
    assert opened["delivery"]["requires_client_reassembly"] is False
    assert opened["receipt"].startswith("pr_")
    assert opened["context_token"].startswith("ct_")
    assert opened["delivery"]["response_bytes"] <= DEFAULT_RESPONSE_BYTES

    view = opened["view"]
    for section in ("binding", "project", "entities", "tracks", "items", "takes", "fx",
                    "routes", "automation", "envelopes", "time", "audio", "analysis",
                    "temporal", "relations", "coverage", "archive", "reading"):
        assert section in view, section
    # Structure is complete and truthful for this capture.
    assert len(view["tracks"]["rows"]) == 3  # two tracks plus master
    assert len(view["analysis"]["rows"]["rows"]) == 3  # one row per captured source
    assert view["analysis"]["metric_columns"]
    catalog = view["archive"]["catalog"]
    assert [row[0] for row in catalog["rows"]].count("series") == 1
    assert view["archive"]["exact"]


def test_mcp_open_defaults_to_the_single_view(tmp_path: Path) -> None:
    from reaper_audio import mcp_server
    from panorama_flow import open_and_ack

    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)

    opened, ack, pages = open_and_ack(
        mcp_server, record.project_id, str(archive.root))

    assert opened["status"] == "page"
    assert opened["page_id"] == opened["manifest"]["page_ids"][0]
    assert opened["view_schema"] == "reaper-workbench.panorama-view/2"
    assert "view" not in opened and "receipt" not in opened
    assert ack["status"] == "complete"
    assert ack["receipt"] and ack["context_token"]
    assert ack["page_count"] == len(pages) == opened["page_count"]


def test_the_view_states_paged_delivery_without_cursor_vocabulary(tmp_path: Path) -> None:
    _record_obj, _archive, _lifecycle, opened = _open(tmp_path)
    text = canonical(opened["view"]).decode("utf-8")

    for forbidden in ('"next_page"', '"cursor":', '"handle_page"', '"pages_remaining"'):
        assert forbidden not in text
    delivery = opened["view"]["reading"]["delivery"]
    assert delivery.startswith("paged MCP delivery")
    assert "fetch every page" in delivery and "ack" in delivery


def test_every_canonical_section_is_placed_or_reported(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)

    view = build_view(document)
    unknown = sorted(set(document) - set(SECTION_INVENTORY))
    assert view["reading_inventory"]["unmapped_sections"] == unknown
    assert not unknown, f"canonical sections with no place in the view: {unknown}"
    # The inventory names every section that reaches the view.
    assert set(SECTION_INVENTORY.values()) >= {
        "binding", "project", "tracks", "items", "takes", "midi", "fx", "routes",
        "automation", "envelopes", "time", "transport", "audio", "analysis",
        "temporal", "relations", "coverage", "archive", "reading",
    }


def test_metric_families_are_complete_and_carry_units(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)
    declared = set(document["analysis_metric_units"])

    view = build_view(document)
    assert set(view["analysis"]["metric_units"]) == declared
    assert len(view["analysis"]["metric_columns"]) == len(declared)
    # The units table is shared, not repeated per row.
    assert all(unit is not None for unit in view["analysis"]["metric_units"].values())


def test_every_present_source_fk_resolves_to_a_stable_track_or_master(
        tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)
    view = build_view(document)
    tracks = view["entities"]["track"]
    sources = [str(row[0]) for row in tracks] + (
        [str(view["entities"]["master"][0])] if view["entities"].get("master") else [])

    assert sources
    for table_name in ("analysis", "audio"):
        table = view[table_name]["rows"]
        rows = table["rows"] if isinstance(table, Mapping) else table
        assert rows
        for row in rows:
            source = row[0]
            assert isinstance(source, int) and source >= 0, (table_name, row)
            assert source < len(sources)
            assert sources[source] in {
                str(item.get("guid")) for item in document["tracks"]
            } | ({str(document["master"].get("guid"))}
                 if document.get("master") else set())

    reported = {row[0] for row in view["analysis"]["rows"]["rows"]}
    assert reported == set(range(len(sources)))


def test_temporal_levels_are_declared_and_never_invented(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)

    view = build_view(document)
    levels = view["temporal"]["levels"]
    assert levels, "the view must state the time levels of the activity grid"
    widths = [level["width_s"] for level in levels]
    assert 300.0 in widths and 30.0 in widths
    for level in levels:
        if level["available"]:
            assert level["aggregation"]
            assert level["rows"]
        else:
            assert level["reason"]


def test_view_is_deterministic(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)

    first = build_view(document)
    second = build_view(document)
    assert first["view_sha256"] == second["view_sha256"]
    assert canonical(first) == canonical(second)
    assert view_bytes(first) == view_bytes(second)


def test_over_budget_is_reported_honestly_and_mints_nothing(tmp_path: Path) -> None:
    _record_obj, archive, lifecycle, _opened = _open(tmp_path)

    result = lifecycle.single("reaper:test", max_response_bytes=2_000)

    assert result["status"] == "response_over_budget"
    assert "view" not in result
    assert "receipt" not in result and "context_token" not in result
    assert "handle" not in result
    limit = result["over_limit"]
    assert limit["measured_bytes"] > limit["max_response_bytes"]
    assert limit["sections_bytes"]
    assert "host_basis" in limit and "what_is_blocking" in limit
    assert isinstance(limit["no_hidden_fallback"], str)
    assert {option["option"] for option in limit["options"]} >= {
        "raise the budget", "read-only exact query with the issued read_scope"}
    assert result.get("read_scope", {}).get("id", "").startswith("rs_")
    # Nothing was left on disk pretending to be a delivered view.
    assert not list((archive.root / "receipts").glob("*.json")) if (
        archive.root / "receipts").is_dir() else True


def test_query_returns_exact_rows_in_one_result(tmp_path: Path) -> None:
    record, _archive, lifecycle, opened = _open(tmp_path)

    result = lifecycle.query(
        opened["handle"], opened["revision"], data=["items"], objects="all")

    assert result["status"] == "query"
    assert result["data"]["items"]
    assert result["budget"]["fits"] is True
    assert result["document_hash"] == opened["document_hash"]


def test_query_rejects_an_unknown_object(tmp_path: Path) -> None:
    _record_obj, _archive, lifecycle, opened = _open(tmp_path)

    with pytest.raises(ValidationError):
        lifecycle.query(opened["handle"], opened["revision"], data=["items"],
                        objects=["track:{NO-SUCH-TRACK}"])


def test_query_over_budget_names_the_scope_to_narrow(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)

    result = run_query(document, record, data=["relations", "analysis"],
                       max_response_bytes=200)

    assert result["status"] == "query_too_large"
    assert result["measured_bytes"] > result["max_response_bytes"]
    assert result["family_bytes"]
    assert result["narrow"]
    assert "paged" in result["note"]


def test_query_still_requires_the_delivered_receipt(tmp_path: Path) -> None:
    _record_obj, archive, lifecycle, opened = _open(tmp_path)
    forged = {**opened["revision"], "document_hash": "0" * 64}

    with pytest.raises(StaleContext):
        lifecycle.query(opened["handle"], forged, data=["items"], objects="all")

    fresh = PanoramaLifecycle(archive.root)
    with pytest.raises(StaleContext):
        fresh.query(opened["handle"], {**opened["revision"], "change_count": 99},
                    data=["items"])


def test_single_receipt_unlocks_mutation_gates(tmp_path: Path) -> None:
    _record_obj, _archive, lifecycle, opened = _open(tmp_path)

    token = lifecycle.require_receipt(
        opened["receipt"], "reaper:test", opened["revision"],
        opened["fact_root"], expected_capture_id=opened["capture_id"])

    assert token["fact_root"] == opened["fact_root"]
    assert token["capture_id"] == opened["capture_id"]
    assert token["audio_evidence"]["state"] == "current"


def test_archive_catalog_measures_every_family(tmp_path: Path) -> None:
    _record_obj, _archive, _lifecycle, opened = _open(tmp_path)

    catalog = opened["view"]["archive"]["catalog"]
    rows = catalog["rows"]
    families = {row[0] for row in rows}
    assert families >= {"relations", "series", "midi_notes", "envelope_points",
                        "items", "takes", "audio", "analysis", "fx", "project_summary"}
    assert catalog["columns"] == ["family", "rows", "row_unit", "bytes", "bytes_method",
                                  "selectors"]
    for row in rows:
        assert isinstance(row[1], int) and row[1] >= 0
        assert isinstance(row[3], int) and row[3] >= 0
        assert row[4] in {"measured", "estimate_from_cell_counts",
                          "estimate_from_note_count", "estimate_from_point_count"}
        assert isinstance(row[5], list)
        if row[0] != "project_summary":
            assert row[5]


def test_view_does_not_invent_interpretation(tmp_path: Path) -> None:
    _record_obj, _archive, _lifecycle, opened = _open(tmp_path)
    view = opened["view"]

    assert view["reading"]["epistemic"]["interpretations"].startswith("empty")
    assert view["reading"]["epistemic"]["hypotheses"].startswith("empty")
    assert view["tracks"]["columns"].count("role") == 1
    assert all(row[-1] is None for row in view["tracks"]["rows"])


def test_document_hash_is_the_sealed_sidecar_hash(tmp_path: Path) -> None:
    from reaper_audio.context.lifecycle import document_hash

    record, _files = _record(tmp_path)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)
    sealed = document["document_sha256"]
    assert document_hash(document) == sealed
    # Tampering is caught at load by the binding, not silently re-hashed.
    from reaper_audio.context.panorama import validate_binding
    from reaper_audio.errors import ReaperAudioError

    changed = dict(document)
    changed["project"] = {**changed["project"], "name": "other"}
    with pytest.raises(ReaperAudioError):
        validate_binding(changed, record)


def test_temporal_levels_declare_that_they_aggregate_stored_cells(tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    view = build_view(build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9))
    stored = view["temporal"]["stored"]
    assert isinstance(stored["anchor_s"], (int, float))
    assert "stored cells" in stored["cell_meaning"]
    assert "exact" in view["temporal"]


def test_catalog_row_counts_name_what_they_count(tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    view = build_view(build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9))
    catalog = view["archive"]["catalog"]
    assert catalog["columns"] == ["family", "rows", "row_unit", "bytes", "bytes_method",
                                  "selectors"]
    rows = {row[0]: row for row in catalog["rows"]}
    assert rows["items"][2] == "rows"
    assert rows["relations"][2] == "edges+pairs+unavailable"
    assert rows["midi_notes"][2] == "notes"
    assert rows["envelope_points"][2] == "points"
    assert rows["series"][2] == "cells"


def test_query_reports_the_point_limit_as_query_too_large(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import reaper_audio.context.query as query_module

    record, _files = _record(tmp_path)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)

    def refuse(*_args, **_kwargs):
        raise ValidationError("too many points", details={"limit": 20_000,
                                                          "requested_series": ["rms_400ms"]})

    monkeypatch.setattr(query_module, "resolve_details", refuse)
    result = run_query(document, record, data=["series"], objects="all")
    assert result["status"] == "query_too_large"
    assert result["reason"] == "exact_detail_point_limit"
    assert result["point_limit"] == 20_000
    assert result["narrow"]


def test_scalar_metrics_complete_and_exact_query_round_trip(tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)
    view = build_view(document)
    analysis = view["analysis"]
    assert len(analysis["metric_columns"]) == 24
    assert len(analysis["metric_units"]) == 24
    assert len(analysis["metric_decimals"]) == 24
    assert analysis["precision"]["lossless"] is False
    assert analysis["rows"]["rows"]
    source = document["analysis"][0]
    metric = next(name for name in analysis["metric_columns"]
                  if isinstance(source["metrics"].get(name), (int, float)))
    exact = run_query(document, record, data=["analysis"],
                      objects=[source["analysis_id"]], metrics=[metric])
    assert exact["status"] == "query"
    assert exact["data"]["analysis"][0]["metrics"][metric] == source["metrics"][metric]


def test_legacy_list_schema_retains_values_and_source_guids(tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    document = json.loads(canonical(build_panorama(
        record, max_bytes=10 ** 9, max_tokens=10 ** 9)))
    columns = ["integrated_lufs", "peak_dbfs"]
    document["analysis_metric_columns"] = columns
    document["analysis_metric_units"] = {"integrated_lufs": "LUFS", "peak_dbfs": "dBFS"}
    for row in document["analysis"]:
        row["metrics"] = [row["metrics"][name] for name in columns]
        row.pop("source_id", None)
        row.pop("stage", None)
        row.pop("channel_view", None)
        row.pop("measured_window", None)
    view = build_view(document)
    analysis = view["analysis"]
    assert analysis["metric_schema"] == "legacy_positional_list"
    assert analysis["metric_columns"] == columns
    assert {row[0] for row in analysis["rows"]["rows"]} == set(
        range(len(document["analysis"])))
    for source, row in zip(document["analysis"], analysis["rows"]["rows"]):
        assert row[0] >= 0
        for index, name in enumerate(columns):
            raw = source["metrics"][index]
            expected = None if raw is None else round(raw, analysis["metric_decimals"][name])
            assert row[6 + index] == expected


def test_synthetic_42_source_lean_view_is_complete_and_bounded(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    document = json.loads(canonical(build_panorama(
        record, max_bytes=10 ** 9, max_tokens=10 ** 9)))
    metric_names = sorted(document["analysis_metric_units"])
    base_track = document["tracks"][0]
    base_analysis = document["analysis"][0]
    base_audio = document["audio"][0]
    master = document["master"]
    tracks = []
    analysis = []
    audio = []
    for index in range(41):
        guid = "{SYNTH-%03d}" % index
        stable = "track:" + guid
        track = {**base_track, "guid": guid, "stable_id": stable,
                 "analysis_id": "analysis:" + guid, "audio_id": "audio:" + guid}
        tracks.append(track)
        row = {**base_analysis, "analysis_id": "analysis:" + guid,
               "source_id": stable, "source_guid": guid,
               "metrics": {name: float(index + offset + 1)
                           for offset, name in enumerate(metric_names)}}
        analysis.append(row)
        audio.append({**base_audio, "source_id": stable, "audio_id": "audio:" + guid,
                      "analysis_id": "analysis:" + guid})
    master_guid = master["guid"]
    master_stable = "master:" + master_guid
    analysis.append({**base_analysis, "analysis_id": "analysis:" + master_guid,
                     "source_id": master_stable, "source_guid": master_guid,
                     "metrics": {name: 100.0 + offset for offset, name in enumerate(metric_names)}})
    audio.append({**base_audio, "source_id": master_stable,
                  "audio_id": "audio:" + master_guid,
                  "analysis_id": "analysis:" + master_guid, "is_master": True})
    document["tracks"] = tracks
    document["analysis"] = analysis
    document["audio"] = audio
    view = build_view(document, lean=True)
    assert view["view_mode"] == "lean_overview"
    assert len(view["tracks"]["rows"]) == 42
    assert len(view["analysis"]["metric_columns"]) == 24
    assert len(view["analysis"]["rows"]["rows"]) == 42
    assert {row[0] for row in view["analysis"]["rows"]["rows"]} == set(range(42))
    assert len(view["content_index"]["rows"]) == 42
    assert view["completeness"]["inline_metrics"]["complete"] is True
    assert view_bytes(view) <= 45_000

    # Full lifecycle envelope on the same deterministic 42-source document.
    from reaper_audio.context.panorama import _hash
    archive = CaptureArchive(tmp_path / "synthetic-archive")
    archive.append(record, files)
    sidecar = archive.captures / record.capture_id / "panorama.json"
    payload = {key: value for key, value in document.items() if key != "document_sha256"}
    document["document_sha256"] = _hash(payload)
    sidecar.write_text(canonical(document).decode("utf-8"), encoding="utf-8")
    opened = PanoramaLifecycle(archive.root, state_root=tmp_path / "state").single(
        record.project_id)
    assert opened["status"] == "complete"
    assert opened["delivery"]["response_bytes"] <= 48_000
    assert len(opened["view"]["analysis"]["rows"]["rows"]) == 42


def _synthetic_100_source_document(tmp_path: Path):
    record, files = _record(tmp_path)
    document = json.loads(canonical(build_panorama(
        record, max_bytes=10 ** 9, max_tokens=10 ** 9)))
    metric_names = sorted(document["analysis_metric_units"])
    base_track = document["tracks"][0]
    base_analysis = document["analysis"][0]
    base_audio = document["audio"][0]
    base_fx = document["fx"][0]
    base_route = document["routes"][0]
    master = document["master"]
    tracks, analysis, audio, fx, routes = [], [], [], [], []
    for index in range(100):
        guid = "{SCALE-%03d}" % index
        stable = "track:" + guid
        tracks.append({**base_track, "guid": guid, "stable_id": stable,
                       "analysis_id": "analysis:" + guid, "audio_id": "audio:" + guid})
        analysis.append({**base_analysis, "analysis_id": "analysis:" + guid,
                         "source_id": stable, "source_guid": guid,
                         "metrics": {name: float(index + offset + 1)
                                     for offset, name in enumerate(metric_names)}})
        audio.append({**base_audio, "source_id": stable, "audio_id": "audio:" + guid,
                      "analysis_id": "analysis:" + guid})
        for slot in range(4):
            fx.append({**base_fx, "owner_id": stable, "fx_id": "fx:" + stable + ":" + str(slot),
                       "slot_index": slot, "name": "Synthetic EQ " + str(index)})
        routes.append({**base_route, "source_id": stable, "destination_id": "master:" + master["guid"]})
    master_guid = master["guid"]
    master_stable = "master:" + master_guid
    analysis.append({**base_analysis, "analysis_id": "analysis:" + master_guid,
                     "source_id": master_stable, "source_guid": master_guid,
                     "metrics": {name: 100.0 + offset for offset, name in enumerate(metric_names)}})
    audio.append({**base_audio, "source_id": master_stable,
                  "audio_id": "audio:" + master_guid,
                  "analysis_id": "analysis:" + master_guid, "is_master": True})
    document["tracks"] = tracks
    document["analysis"] = analysis
    document["audio"] = audio
    document["fx"] = fx
    document["routes"] = routes
    return record, files, document


def test_100_source_overbudget_issues_read_scope_and_refuses_oversized_query(
        tmp_path: Path) -> None:
    record, files, document = _synthetic_100_source_document(tmp_path)
    assert len(document["tracks"]) + 1 == 101
    lean = build_view(document, lean=True)
    assert view_bytes(lean) > 48_000
    assert len(lean["analysis"]["metric_columns"]) == 24
    archive = CaptureArchive(tmp_path / "scale-archive")
    archive.append(record, files)
    from reaper_audio.context.panorama import _hash
    payload = {key: value for key, value in document.items() if key != "document_sha256"}
    document["document_sha256"] = _hash(payload)
    (archive.captures / record.capture_id / "panorama.json").write_text(
        canonical(document).decode("utf-8"), encoding="utf-8")
    opened = PanoramaLifecycle(archive.root, state_root=tmp_path / "state").single(
        record.project_id)
    assert opened["status"] == "response_over_budget"
    assert "view" not in opened
    assert "receipt" not in opened and "context_token" not in opened and "handle" not in opened
    scope = opened["read_scope"]
    source = document["analysis"][0]
    small = PanoramaLifecycle(archive.root, state_root=tmp_path / "state").query(
        "", opened["revision"], data=["analysis"], objects=[source["analysis_id"]],
        metrics=["crest_db"], read_scope=scope["id"])
    assert small["status"] == "query"
    assert small["read_only"] is True
    oversized = PanoramaLifecycle(archive.root, state_root=tmp_path / "state").query(
        "", opened["revision"], data=["analysis"], objects="all",
        read_scope=scope["id"])
    assert oversized["status"] == "query_too_large"
    assert "data" not in oversized
