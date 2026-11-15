"""Exact behaviour of the audio-snapshot delta.

These tests use stored report fragments with a known time axis, so every
reported interval, cell and unit can be checked by hand.  They deliberately do
not exercise a render or a project change: the delta compares measurements.
"""

from __future__ import annotations

import pytest

from reaper_audio.context.audio_delta import build_audio_delta
from reaper_audio.errors import ValidationError

TIMES = [0.0, 0.4, 0.8, 1.2]


def _report(*, lufs=-14.0, peak=-1.0, peak_unit="dBFS", rms=None, times=None,
            band=None, profiles=None, window=(0.0, 4.0), extra_metric=None):
    times = list(TIMES if times is None else times)
    rms = [-20.0, -20.0, -20.0, -20.0] if rms is None else rms
    source = {
        "metrics": {
            "integrated_lufs": {"value": lufs, "unit": "LUFS"},
            "peak_dbfs": {"value": peak, "unit": peak_unit},
        },
        "curves": {
            "rms_400ms_time_s": times,
            "rms_400ms_dbfs": rms,
        },
    }
    if band is not None:
        source["curves"]["spectrogram_time_s"] = times
        source["curves"]["band_level_time_dbfs"] = band
    if profiles is not None:
        source["curves"]["temporal_profile"] = {"times": times, "cells": profiles}
    if extra_metric:
        source["metrics"].update(extra_metric)
    return {
        "project": {"project_id": "reaper:test"},
        "section": {"start_s": window[0], "end_s": window[1]},
        "sources": {"G1": source},
    }


def _record(report, *, analysis_id="a1", capture_id="cap1", change=5,
            instance_id="bridge", epoch=1, path="C:/songs/test.rpp"):
    return {
        "analysis_id": analysis_id,
        "project_id": "reaper:test",
        "capture_id": capture_id,
        "revision": {"project_id": "reaper:test", "path": path,
                     "change_count": change,
                     "instance_id": instance_id, "epoch": epoch},
        "report": report,
    }


def test_changed_cells_become_exact_intervals() -> None:
    base = _record(_report(rms=[-20.0, -20.0, -20.0, -20.0]))
    target = _record(_report(rms=[-20.0, -18.0, -18.0, -20.0]), analysis_id="a2",
                     capture_id="cap2", change=6)

    delta = build_audio_delta(base, target)

    series = delta["series"]["rows"][0]
    assert series["status"] == "compared"
    assert series["unit"] == "dBFS"
    assert series["compared_cells"] == 4
    assert series["changed_cells"] == 2
    assert series["changed_intervals"] == [
        {"start_s": 0.4, "end_s": 1.2, "cells": 2, "first_cell": 1, "last_cell": 2,
         "end_basis": "next_stored_cell_start"},
    ]
    assert [cell["delta"] for cell in series["cells"]] == [2.0, 2.0]
    assert [cell["start_s"] for cell in series["cells"]] == [0.4, 0.8]
    assert series["cells_complete"] is True
    assert series["changed_intervals_unbounded_cells"] == 0
    assert series["magnitude"]["count"] == 2
    assert series["magnitude"]["max_abs"] == 2.0


def test_metrics_keep_units_and_report_nulls_and_incompatibility() -> None:
    base = _record(_report(lufs=-14.0, peak=-1.0))
    target = _record(_report(lufs=-12.5, peak=None, peak_unit="dBTP"), analysis_id="a2")

    delta = build_audio_delta(base, target)

    rows = {(row["metric"]): row for row in delta["metrics"]["rows"]}
    assert rows["integrated_lufs"] == {
        "source": "G1", "metric": "integrated_lufs", "unit": "LUFS",
        "before": -14.0, "after": -12.5, "delta": 1.5,
    }
    # peak_dbfs is null on one side, so it is missing, not a zero delta.
    assert delta["metrics"]["missing"] == [
        {"source": "G1", "metric": "peak_dbfs", "before": {"value": -1.0, "unit": "dBFS"},
         "after": {"value": None, "unit": "dBTP"}},
    ]
    assert delta["metrics"]["incompatible"] == []


def test_unit_mismatch_is_not_compared() -> None:
    base = _record(_report(extra_metric={"loudness": {"value": 1.0, "unit": "LU"}}))
    target = _record(_report(extra_metric={"loudness": {"value": 2.0, "unit": "dB"}}),
                     analysis_id="a2")

    delta = build_audio_delta(base, target, metrics=["loudness"])

    assert delta["metrics"]["rows"] == []
    assert delta["metrics"]["incompatible"] == [
        {"source": "G1", "metric": "loudness", "before_unit": "LU", "after_unit": "dB"},
    ]


def test_time_axis_mismatch_refuses_a_pointwise_delta() -> None:
    base = _record(_report())
    target = _record(_report(times=[0.0, 0.5, 1.0, 1.5], rms=[-20.0] * 4),
                     analysis_id="a2")

    delta = build_audio_delta(base, target)

    series = delta["series"]["rows"][0]
    assert series["status"] == "incompatible"
    assert series["reason"] == "time_axis_differs"
    assert "cells" not in series
    assert series["before"]["count"] == series["after"]["count"] == 4
    assert "pointwise delta would be fictitious" in series["note"]


def test_window_mismatch_raises_instead_of_comparing() -> None:
    base = _record(_report(window=(0.0, 4.0)))
    target = _record(_report(window=(0.0, 3.0)), analysis_id="a2")

    with pytest.raises(ValidationError, match="different windows"):
        build_audio_delta(base, target)


def test_interval_filter_and_complete_intervals_when_cells_are_bounded() -> None:
    base = _record(_report(rms=[-20.0, -20.0, -20.0, -20.0]))
    target = _record(_report(rms=[-10.0, -11.0, -12.0, -20.0]), analysis_id="a2")

    delta = build_audio_delta(base, target, max_changed_cells=1,
                              interval={"start_s": 0.2, "end_s": 1.2})

    series = delta["series"]["rows"][0]
    assert series["compared_cells"] == 2
    assert series["changed_cells"] == 2
    assert series["changed_intervals"] == [
        {"start_s": 0.4, "end_s": 1.2, "cells": 2, "first_cell": 1, "last_cell": 2,
         "end_basis": "next_stored_cell_start"},
    ]
    # The legacy argument no longer hides cells; every changed row is present.
    assert series["cells_complete"] is True
    assert series["cells_omitted"] == 0
    assert len(series["cells"]) == 2


def test_a_final_cell_uses_the_window_end_and_never_an_inferred_width() -> None:
    base = _record(_report(rms=[-20.0, -20.0, -20.0, -20.0]))
    target = _record(_report(rms=[-20.0, -20.0, -20.0, -10.0]), analysis_id="a2")

    delta = build_audio_delta(base, target)

    series = delta["series"]["rows"][0]
    assert series["changed_intervals"] == [
        {"start_s": 1.2, "end_s": 4.0, "cells": 1, "first_cell": 3, "last_cell": 3,
         "end_basis": "measurement_window_end"},
    ]
    assert series["changed_intervals_unbounded_cells"] == 0


def test_without_a_proven_boundary_a_final_cell_stays_a_point() -> None:
    report = _report(rms=[-20.0, -20.0, -20.0, -20.0])
    report["section"] = {}
    base = _record(report)
    target_report = _report(rms=[-20.0, -20.0, -20.0, -10.0])
    target_report["section"] = {}
    target = _record(target_report, analysis_id="a2")

    delta = build_audio_delta(base, target)

    series = delta["series"]["rows"][0]
    assert series["changed_intervals"] == []
    assert series["changed_intervals_unbounded_cells"] == 1
    assert series["cells"][0]["start_s"] == 1.2


def test_irregular_axis_uses_stored_boundaries_not_a_median_width() -> None:
    times = [0.0, 0.3, 0.9, 2.2]
    base = _record(_report(times=times, rms=[-20.0] * 4))
    target = _record(_report(times=times, rms=[-20.0, -18.0, -18.0, -20.0]),
                     analysis_id="a2")

    delta = build_audio_delta(base, target)

    series = delta["series"]["rows"][0]
    assert series["changed_intervals"] == [
        {"start_s": 0.3, "end_s": 2.2, "cells": 2, "first_cell": 1, "last_cell": 2,
         "end_basis": "next_stored_cell_start"},
    ]
    assert series["axis"]["boundary_basis"].startswith("cell starts are stored values")
    assert "cell_width_s" not in series["axis"]


def test_identity_mismatch_refuses_the_delta() -> None:
    base = _record(_report())
    target = _record(_report(), analysis_id="a2", epoch=2)

    with pytest.raises(ValidationError, match="not one continuous project history"):
        build_audio_delta(base, target)


def test_missing_instance_identity_refuses_the_delta() -> None:
    report = _report()
    record = _record(report)
    record["revision"].pop("instance_id")

    with pytest.raises(ValidationError, match="continuity is unproven"):
        build_audio_delta(record, record)


def test_band_level_vector_delta_reports_every_band() -> None:
    base = _record(_report(band=[[-30.0, -20.0]] * 4))
    target = _record(_report(band=[[-28.0, -20.0], [-28.0, -18.0],
                                  [-30.0, -20.0], [-30.0, -20.0]]), analysis_id="a2")

    delta = build_audio_delta(base, target, series=["band_level"])

    series = delta["series"]["rows"][0]
    assert series["status"] == "compared"
    assert series["changed_cells"] == 2
    assert series["cells"][0]["delta"] == [2.0, 0.0]
    assert series["cells"][1]["delta"] == [2.0, 2.0]
    assert series["magnitude"]["max_abs"] == 2.0


def test_temporal_profile_cells_compare_structurally() -> None:
    base_cells = [{"short_term_lufs": -20.0}, {"short_term_lufs": -20.0},
                  {"short_term_lufs": -20.0}, {"short_term_lufs": -20.0}]
    target_cells = [{"short_term_lufs": -20.0}, {"short_term_lufs": -18.0},
                    {"short_term_lufs": -20.0}, {"short_term_lufs": -20.0}]
    base = _record(_report(profiles=base_cells))
    target = _record(_report(profiles=target_cells), analysis_id="a2")

    delta = build_audio_delta(base, target, series=["temporal_profile"])

    series = delta["series"]["rows"][0]
    assert series["changed_cells"] == 1
    assert series["changed_intervals"][0]["start_s"] == 0.4
    assert series["magnitude"] == {"kind": "structured_cells", "delta": None}
    assert series["cells"][0]["before"] == {"short_term_lufs": -20.0}
    assert series["cells"][0]["after"] == {"short_term_lufs": -18.0}


def test_target_capture_can_be_marked_stale() -> None:
    base = _record(_report())
    target = _record(_report(), analysis_id="a2", capture_id="cap2")

    delta = build_audio_delta(base, target, current_capture_id="cap3")

    assert delta["freshness"]["state"] == "stale_target"
    assert "older audio state" in delta["freshness"]["reason"]
    current = build_audio_delta(base, target, current_capture_id="cap2")
    assert current["freshness"]["state"] == "current"


def test_more_than_two_thousand_changed_cells_are_all_delivered_paged(tmp_path) -> None:
    from reaper_audio.context.lifecycle import PanoramaLifecycle, _merge_view_pages

    times = [index * 0.01 for index in range(2500)]
    base = _record(_report(times=times, rms=[-20.0] * len(times), window=(0.0, 25.0)))
    target = _record(_report(times=times, rms=[-19.0] * len(times), window=(0.0, 25.0)),
                     analysis_id="a2", capture_id="cap2", change=6)
    delta = build_audio_delta(base, target)
    series = delta["series"]["rows"][0]
    assert series["changed_cells"] == 2500
    assert series["cells_complete"] is True
    assert series["cells_omitted"] == 0
    assert len(series["cells"]) == 2500

    lifecycle = PanoramaLifecycle(tmp_path / "archive", state_root=tmp_path / "state")
    page = lifecycle.open_result_pages(
        delta, result_kind="audio_delta", project_id="reaper:test",
        capture_id="cap2", revision=target["revision"],
        fact_root_value="", document_hash_value="")
    assert page["status"] == "result_page"
    assert page["result_kind"] == "audio_delta"
    ids = page["manifest"].get("page_ids") or [
        f"rp_{index}_of_{page['manifest']['page_count']}"
        for index in range(1, page["manifest"]["page_count"] + 1)]
    pages = [page] + [lifecycle.view_page(page["handle"], page["revision"], page_id)
                      for page_id in ids[1:]]
    merged = dict(_merge_view_pages(pages))
    assert len(merged["series"]["rows"][0]["cells"]) == 2500
    ack = lifecycle.ack_pages(page["handle"], page["revision"],
                              [item["page_hash"] for item in pages])
    assert ack["status"] == "complete"
    assert ack["context_token"] is None
    assert ack["mutation_unlocked"] is False
