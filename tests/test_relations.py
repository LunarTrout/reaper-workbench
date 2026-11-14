"""Focused tests for the canonical cross-source relation model."""

from __future__ import annotations

import dataclasses
import json

import pytest

from reaper_audio.analysis.relations import (
    band_dominance_candidates,
    band_dominance_interval,
    merge_batches,
    phase_candidates,
    reference_candidates,
    top_band_dominance,
    track_master_candidates,
)
from reaper_audio.context.errors import ContractValidationError
from reaper_audio.context.ids import is_derived_prefix
from reaper_audio.context.relations import (
    RELATION_KINDS,
    RelationBinding,
    RelationWindow,
    build_relation_set,
)


def _binding(**overrides) -> RelationBinding:
    values = dict(
        capture_id="cap_01TEST",
        project_id="prj_01TEST",
        revision_fingerprint="rev-fingerprint-1",
        fact_root="fact-root-1",
        window=RelationWindow(
            start_s=0.0,
            end_s=10.0,
            start_sample=0,
            end_sample=480_000,
            sample_rate_hz=48_000,
        ),
        stage="track_post_fx",
        node="track",
        channel_view="stereo",
        render_key="render-key-1",
    )
    values.update(overrides)
    return RelationBinding(**values)


def _band_sources(peak_db: float = 16.0) -> dict:
    high = [[peak_db, 0.0, 0.0]] * 4
    zero = [[0.0, 0.0, 0.0]] * 4
    return {
        "src_A": {"bands": high, "band_centers_hz": [100.0, 1000.0, 5000.0], "pan": -0.8},
        "src_B": {"bands": zero, "band_centers_hz": [100.0, 1000.0, 5000.0], "pan": -0.8},
    }


def _phase_result() -> dict:
    return {
        ("src_A", "src_B"): {
            "coverage": {"status": "available"},
            "channel_pairs": [
                {"zero_lag_correlation": 0.90, "power_change_vs_incoherent_sum_db": -1.2},
                {"zero_lag_correlation": 0.88, "power_change_vs_incoherent_sum_db": -1.0},
            ],
            "resampled": {"a": False, "b": False},
        }
    }


def _batch():
    band = band_dominance_candidates(_band_sources())
    phase = phase_candidates(_phase_result())
    master = track_master_candidates(
        {"src_A": {"rms_dbfs": -20.0}, "src_B": {"rms_dbfs": -20.0}},
        {"rms_dbfs": -14.0, "integrated_lufs": -14.0},
        master_id="master:1",
    )
    references = reference_candidates(
        {"metrics": {
            "integrated_lufs": {"value": -14.0, "unit": "LUFS"},
            "rms_dbfs": {"value": -18.0, "unit": "dBFS"},
        }},
        [
            {"reference_id": "ref_1", "stage": "master_post_fx",
             "metrics": {"integrated_lufs": {"value": -13.0, "unit": "LUFS"},
                         "rms_dbfs": {"value": -17.0, "unit": "dBFS"}}},
            {"reference_id": "ref_2", "stage": "track_post_fx",
             "metrics": {"integrated_lufs": {"value": -13.0, "unit": "LUFS"}}},
        ],
        stage="master_post_fx",
    )
    return merge_batches(band, phase, master, references)


def _assets() -> dict:
    return {"src_A": "asset_a", "src_B": "asset_b", "master:1": "asset_master"}


def _build():
    return build_relation_set(
        _binding(),
        _batch(),
        source_assets=_assets(),
        known_ids=["src_A", "src_B", "master:1"],
        master_id="master:1",
    )


def _all_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(_all_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_all_keys(item))
    return keys


def test_rel_is_a_registered_derived_prefix() -> None:
    assert is_derived_prefix("rel")
    assert "band_dominance" in RELATION_KINDS
    assert "band_dominance_pair" in RELATION_KINDS
    assert "track_master_level_relation" in RELATION_KINDS
    assert "track_master_contribution" not in RELATION_KINDS


def test_every_producer_kind_round_trips_into_a_typed_record() -> None:
    relation_set = _build()
    kinds = {relation.kind for relation in relation_set.relations}
    assert kinds == {
        "band_dominance",
        "phase_pair",
        "track_master_level_relation",
        "reference_comparison",
    }
    for relation in relation_set.relations:
        assert relation.relation_id.startswith("rel_")
        assert relation.stage == "track_post_fx"
        assert relation.window == _binding().window
        assert relation.provenance["capture_id"] == "cap_01TEST"
        assert relation.dep_set["asset_ids"]
        assert len(relation.alternatives) >= 2
        assert relation.units.keys() == relation.metrics.keys()


def test_unavailable_pair_row_keeps_a_typed_address_and_series_ref() -> None:
    sources = {
        "src_A": {"bands": [[10.0, 0.0]] * 2, "band_centers_hz": [100.0, 1000.0]},
        "src_B": {"bands": [[0.0, 0.0, 0.0]] * 2,
                  "band_centers_hz": [100.0, 1000.0, 5000.0]},
    }
    relation_set = build_relation_set(
        _binding(),
        band_dominance_candidates(sources),
        source_assets={"src_A": "asset-a", "src_B": "asset-b"},
    )
    table = relation_set.evaluations["band_dominance_pairs"]
    row = dict(zip(table["columns"], table["rows"][0]))
    assert row["status"] == "unavailable"
    assert row["reason"] == "band_count_mismatch"
    assert row["relation_id"].startswith("rel_")
    assert row["dep_set"]["asset_ids"] == ["asset-a", "asset-b"]
    assert row["series_ref"] == "band_pair:src_A|src_B"
    assert table["temporal_model"]["median_is_complete_temporal_relation"] is False


def test_relation_id_changes_with_the_capture_render_key() -> None:
    first = _build()
    second = build_relation_set(
        _binding(render_key="render-key-2"),
        _batch(),
        source_assets=_assets(),
        known_ids=["src_A", "src_B", "master:1"],
        master_id="master:1",
    )
    first_ids = {item.relation_id for item in first.relations}
    second_ids = {item.relation_id for item in second.relations}
    assert first_ids.isdisjoint(second_ids)
    first_row = dict(zip(
        first.evaluations["band_dominance_pairs"]["columns"],
        first.evaluations["band_dominance_pairs"]["rows"][0],
    ))
    second_row = dict(zip(
        second.evaluations["band_dominance_pairs"]["columns"],
        second.evaluations["band_dominance_pairs"]["rows"][0],
    ))
    assert first_row["relation_id"] != second_row["relation_id"]


def test_missing_source_rms_is_counted_as_not_evaluated() -> None:
    batch = track_master_candidates(
        {"src_A": {"rms_dbfs": -20.0}, "src_B": {"integrated_lufs": -18.0}},
        {"rms_dbfs": -14.0, "integrated_lufs": -14.0},
        master_id="master:1",
    )
    assert batch.expected["track_master_level_relation"] == 2
    assert batch.evaluated["track_master_level_relation"] == 1
    assert batch.observed["track_master_level_relation"] == 1
    assert batch.unavailable["track_master_level_relation"] == "some_sources_missing_rms"


def test_interval_band_dominance_uses_only_selected_frames() -> None:
    times = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
    source_a = {
        "bands": [[10.0, 0.0]] * len(times),
        "times": times,
        "band_centers_hz": [100.0, 1000.0],
        "pan": -0.8,
    }
    source_b = {
        "bands": [[10.0, 0.0], [10.0, 0.0], [10.0, 0.0], [10.0, 0.0],
                  [-30.0, 0.0], [10.0, 0.0]],
        "times": times,
        "band_centers_hz": [100.0, 1000.0],
        "pan": -0.8,
    }
    whole = band_dominance_candidates({"src_A": source_a, "src_B": source_b})
    assert whole.observed["band_dominance"] == 0

    interval = band_dominance_interval(
        {"src_A": source_a, "src_B": source_b}, start_s=36.0, end_s=45.0
    )
    table = interval.tables["band_dominance_pairs"]
    row = dict(zip(table["columns"], table["rows"][0]))
    assert row["status"] == "evaluated"
    assert row["best_positive_db"] == 20.0
    assert table["interval"]["sources"]["src_A"]["selected_frames"] == 1
    assert table["interval"]["sources"]["src_B"]["selected_frames"] == 1
    assert table["interval"]["sources"]["src_A"]["partial_start"] is True
    assert table["interval"]["sources"]["src_A"]["partial_end"] is True
    assert interval.observed["band_dominance"] == 1


def test_interval_without_frames_is_explicitly_unavailable() -> None:
    sources = {
        "src_A": {"bands": [[10.0, 0.0]] * 2, "times": [0.0, 1.0],
                  "band_centers_hz": [100.0, 1000.0]},
        "src_B": {"bands": [[0.0, 0.0]] * 2, "times": [0.0, 1.0],
                  "band_centers_hz": [100.0, 1000.0]},
    }
    batch = band_dominance_interval(sources, start_s=36.0, end_s=45.0)
    table = batch.tables["band_dominance_pairs"]
    row = dict(zip(table["columns"], table["rows"][0]))
    assert row["status"] == "unavailable"
    assert row["reason"] == "no_frames_in_interval"
    assert batch.unavailable["band_dominance:src_A"] == "no_frames_in_interval"
    assert batch.evaluated["band_dominance"] == 0


def test_unavailable_source_still_gets_pair_rows() -> None:
    sources = {
        "src_A": {"bands": [[10.0, 0.0], [9.0, 0.0]]},
        "src_B": {"bands": [[0.0]]},
        "src_C": {"bands": [[8.0, 0.0], [7.0, 0.0]]},
    }
    batch = band_dominance_candidates(sources)
    table = batch.tables["band_dominance_pairs"]
    assert table["expected_pairs"] == 3
    assert len(table["rows"]) == 3
    rows = [dict(zip(table["columns"], row)) for row in table["rows"]]
    assert {row["status"] for row in rows if "src_B" in (row["from_id"], row["to_id"])} == {
        "unavailable"
    }
    assert any(row["status"] in {"evaluated", "no_dominant_band"} for row in rows)


def test_band_table_preserves_every_pair_and_a_stable_id() -> None:
    relation_set = _build()
    table = relation_set.evaluations["band_dominance_pairs"]
    assert table["canonical"] is True
    assert table["expected_pairs"] == 1
    assert table["evaluated_pairs"] == 1
    assert len(table["rows"]) == 1
    columns = table["columns"]
    row = dict(zip(columns, table["rows"][0]))
    assert row["status"] == "evaluated"
    assert row["relation_id"].startswith("rel_")
    assert row["dep_set"]["asset_ids"] == ["asset_a", "asset_b"]
    assert row["series_ref"] == "band_pair:src_A|src_B"
    assert table["temporal_model"]["row_series_ref"] == "band_pair:<from_id>|<to_id>"
    assert table["temporal_model"]["source_series"].endswith("/curves/band_level_time_dbfs")
    assert table["temporal_model"]["median_is_complete_temporal_relation"] is False
    band_relation = next(
        item for item in relation_set.relations if item.kind == "band_dominance"
    )
    assert band_relation.attributes["temporal_evidence"][
        "median_is_complete_temporal_relation"
    ] is False


def test_no_dominant_pair_is_kept_as_evaluated_evidence() -> None:
    batch = band_dominance_candidates(_band_sources(peak_db=14.5))
    assert batch.candidates == ()
    assert batch.expected["band_dominance"] == 1
    assert batch.evaluated["band_dominance"] == 1
    table = batch.tables["band_dominance_pairs"]
    row = dict(zip(table["columns"], table["rows"][0]))
    assert row["status"] == "no_dominant_band"
    assert row["best_positive_db"] < 10.0
    assert batch.complete is True
    relation_set = build_relation_set(
        _binding(), batch, source_assets=_assets(),
        known_ids=["src_A", "src_B"], master_id="master:1",
    )
    assert relation_set.relations == ()
    assert relation_set.coverage["band_dominance"]["status"] == "complete"
    assert relation_set.coverage["band_dominance"]["evaluated"] == 1


def test_band_relation_carries_pan_relation_and_no_threshold_claim() -> None:
    relation_set = _build()
    band = next(item for item in relation_set.relations if item.kind == "band_dominance")
    assert band.attributes["pan_relation"] == "same_side"
    assert band.attributes["is_psychoacoustic_threshold"] is False
    assert band.metrics["band_delta_db"] >= 10.0


def test_track_master_is_a_level_relation_not_a_causal_contribution() -> None:
    relation_set = _build()
    relations = [
        item for item in relation_set.relations
        if item.kind == "track_master_level_relation"
    ]
    assert len(relations) == 2
    for relation in relations:
        assert "contribution" not in relation.kind
        assert relation.attributes["causal_contribution"] is False
        assert relation.attributes["metric_family"] == "summed_source_power_share"
        assert relation.attributes["master_bus_nonlinearity"] == "not_modelled"
        assert "not a causal contribution" in relation.interpretation
        assert relation.metrics["share_of_summed_power_pct"] == pytest.approx(50.0)


def test_reference_stage_mismatch_is_explicit_and_does_not_block_mandatory_completeness() -> None:
    relation_set = _build()
    refs = [
        item for item in relation_set.relations
        if item.kind == "reference_comparison"
    ]
    assert len(refs) == 1
    assert refs[0].to_id == "ref_1"
    assert relation_set.unavailable["reference_comparison:ref_2"] == "stage_mismatch"
    coverage = relation_set.coverage["reference_comparison"]
    assert coverage["status"] == "partial"
    assert coverage["expected"] == 2
    assert coverage["evaluated"] == 2
    assert coverage["observed"] == 1
    assert relation_set.is_complete is True


def test_relation_ids_are_reproducible_and_revision_bound() -> None:
    first = _build()
    second = _build()
    assert [item.relation_id for item in first.relations] == [
        item.relation_id for item in second.relations
    ]
    other_revision = build_relation_set(
        _binding(revision_fingerprint="rev-fingerprint-2", render_key=None),
        _batch(),
        source_assets=_assets(),
        known_ids=["src_A", "src_B", "master:1"],
        master_id="master:1",
    )
    assert {item.relation_id for item in first.relations}.isdisjoint(
        {item.relation_id for item in other_revision.relations}
    )
    other_window = build_relation_set(
        _binding(window=RelationWindow(start_s=0.0, end_s=11.0)),
        _batch(),
        source_assets=_assets(),
        known_ids=["src_A", "src_B", "master:1"],
        master_id="master:1",
    )
    assert {item.relation_id for item in first.relations}.isdisjoint(
        {item.relation_id for item in other_window.relations}
    )


def test_table_row_ids_are_stable_across_rebuilds() -> None:
    first = _build().evaluations["band_dominance_pairs"]
    second = _build().evaluations["band_dominance_pairs"]
    first_row = dict(zip(first["columns"], first["rows"][0]))
    second_row = dict(zip(second["columns"], second["rows"][0]))
    assert first_row["relation_id"] == second_row["relation_id"]


def test_top_k_is_only_a_partial_convenience_view() -> None:
    high = [[30.0, 0.0, 0.0]] * 2 + [[0.0, 0.0, 0.0]] * 2
    zero = [[0.0, 0.0, 0.0]] * 4
    sources = {
        "src_A": {"bands": high, "pan": -0.8},
        "src_B": {"bands": zero, "pan": -0.8},
        "src_C": {"bands": zero, "pan": -0.8},
    }
    batch = band_dominance_candidates(sources)
    assert batch.expected["band_dominance"] == 3
    assert batch.evaluated["band_dominance"] == 3
    assert len(batch.candidates) == 2
    view = top_band_dominance(batch, limit=1)
    assert view.complete is False
    assert view.truncated["dropped"] == 1
    assert len(view.candidates) == 1
    relation_set = build_relation_set(
        _binding(), batch, source_assets=_assets(),
        known_ids=["src_A", "src_B", "src_C"], master_id="master:1",
    )
    projection = relation_set.projection(limit=1)
    assert projection["complete"] is False
    assert projection["reason"] == "bounded_projection"
    assert projection["truncated"]["dropped"] >= 1
    assert projection["canonical_content_hash"] == relation_set.content_hash()


def test_serialized_relation_set_has_no_score_or_verdict_keys() -> None:
    payload = _build().to_dict()
    keys = _all_keys(payload)
    assert "score" not in keys
    assert "better" not in keys
    assert "worse" not in keys
    assert payload["complete"] is True
    assert json.loads(json.dumps(payload)) == payload


def test_record_validation_rejects_missing_units_and_alternatives() -> None:
    batch = _batch()
    candidate = next(item for item in batch.candidates if item.kind == "band_dominance")
    bad_units = dataclasses.replace(candidate, units={})
    with pytest.raises(ContractValidationError):
        build_relation_set(
            _binding(),
            dataclasses.replace(batch, candidates=(bad_units,) + batch.candidates[1:]),
            source_assets=_assets(),
            known_ids=["src_A", "src_B", "master:1"],
            master_id="master:1",
        )
    bad_alternatives = dataclasses.replace(candidate, alternatives=("only one",))
    with pytest.raises(ContractValidationError):
        build_relation_set(
            _binding(),
            dataclasses.replace(batch, candidates=(bad_alternatives,) + batch.candidates[1:]),
            source_assets=_assets(),
            known_ids=["src_A", "src_B", "master:1"],
            master_id="master:1",
        )


def test_unknown_endpoint_is_rejected() -> None:
    with pytest.raises(ContractValidationError):
        build_relation_set(
            _binding(),
            _batch(),
            source_assets=_assets(),
            known_ids=["src_A", "master:1"],
            master_id="master:1",
        )
