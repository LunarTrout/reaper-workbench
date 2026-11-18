"""Stage C2–C4 acceptance: decision classes, citations, blockers."""

from __future__ import annotations

import pytest

from reaper_audio.context.contract_v2 import stale_fraction_threshold
from reaper_audio.context.coverage import interval_coverage, uncovered_spans
from reaper_audio.context.decision_gates import MAX_BLOCKERS, evaluate

UNITS = {
    "ev_rms_1": {"evidence_id": "ev_rms_1", "metric": "rms_dbfs", "value": -18.25},
    "ev_rms_2": {"evidence_id": "ev_rms_2", "metric": "rms_dbfs", "value": -19.0},
}
LOCAL_COVERAGE = {
    "complete": False, "measured_sources": ["track:A"], "sources_measured": 1,
    "total_sources": 42, "missing_sources": ["track:B"],
}
GLOBAL_PARTIAL = {
    "complete": False, "measured_sources": ["track:A"], "sources_measured": 12,
    "total_sources": 42, "missing_sources": ["track:B"],
}
GLOBAL_FULL = {
    "complete": True, "measured_sources": ["track:A", "track:B"],
    "sources_measured": 42, "total_sources": 42, "missing_sources": [],
}


def test_forged_citation_is_refused() -> None:
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      basis=[{"evidence_id": "ev_rms_1", "value": -12.0}],
                      units=UNITS, coverage=LOCAL_COVERAGE)
    assert report["ok"] is False
    assert [b["code"] for b in report["blockers"]] == ["basis_mismatch"]
    assert "-12.0" in report["blockers"][0]["message"]


def test_correct_citation_passes() -> None:
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      basis=[{"evidence_id": "ev_rms_1", "value": -18.25}],
                      units=UNITS, coverage=LOCAL_COVERAGE)
    assert report["ok"] is True, report["blockers"]
    assert report["decision_class"] == "local"


def test_local_edit_with_fresh_evidence_needs_no_full_capture() -> None:
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      basis=[{"evidence_id": "ev_rms_1", "value": -18.25}],
                      units=UNITS, coverage=LOCAL_COVERAGE)
    assert report["ok"] is True
    assert not any(b["code"] == "coverage_incomplete" for b in report["blockers"])


def test_global_claim_on_partial_coverage_is_refused_with_numbers() -> None:
    report = evaluate(requested_class="global", target_sources=["track:A"],
                      basis=[{"evidence_id": "ev_rms_1", "value": -18.25}],
                      units=UNITS, coverage=GLOBAL_PARTIAL)
    assert report["ok"] is False
    assert report["blockers"][0]["code"] == "coverage_incomplete"
    assert "12/42" in report["blockers"][0]["message"]


def test_global_claim_on_full_coverage_passes() -> None:
    report = evaluate(requested_class="global", target_sources=["track:A"],
                      basis=[], units=UNITS, coverage=GLOBAL_FULL)
    assert report["ok"] is True, report["blockers"]


def test_master_edit_is_promoted_and_never_lowered() -> None:
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      master_touched=True, basis=[], units=UNITS,
                      coverage=GLOBAL_PARTIAL)
    assert report["decision_class"] == "global"
    assert report["promoted"] == ["global"]
    assert report["ok"] is False  # promoted claim now needs a complete passport


def test_global_request_is_not_lowered_by_absent_promotions() -> None:
    report = evaluate(requested_class="global", target_sources=[], basis=[],
                      units=UNITS, coverage=GLOBAL_FULL)
    assert report["decision_class"] == "global"
    assert report["promoted"] == []


def test_blockers_never_exceed_three() -> None:
    basis = [{"evidence_id": f"ev_missing_{index}", "value": 1.0} for index in range(5)]
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      basis=basis, units=UNITS, coverage=LOCAL_COVERAGE)
    assert len(report["blockers"]) == MAX_BLOCKERS == 3
    assert report["blockers_truncated"] is True
    assert all(blocker["resolution"] for blocker in report["blockers"])


def test_stale_fraction_promotes_to_global() -> None:
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      basis=[], units=UNITS, coverage=GLOBAL_FULL,
                      stale_fraction=0.5, stale_fraction_threshold=0.25)
    assert report["decision_class"] == "global"
    assert "global" in report["promoted"]


def test_metric_mismatch_is_refused() -> None:
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      basis=[{"evidence_id": "ev_rms_1", "metric": "lufs_i"}],
                      units=UNITS, coverage=LOCAL_COVERAGE)
    assert report["ok"] is False
    assert report["blockers"][0]["code"] == "basis_mismatch"


def test_stale_threshold_default_is_fifty_percent(monkeypatch) -> None:
    monkeypatch.delenv("REAPER_V2_STALE_FRACTION_THRESHOLD", raising=False)
    assert stale_fraction_threshold() == pytest.approx(0.5)


def test_stale_threshold_hundred_disables_the_trigger(monkeypatch) -> None:
    monkeypatch.setenv("REAPER_V2_STALE_FRACTION_THRESHOLD", "100")
    assert stale_fraction_threshold() is None
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      units=UNITS, coverage=LOCAL_COVERAGE, stale_fraction=0.9)
    assert report["decision_class"] == "local"
    assert report["promoted"] == []


def test_stale_threshold_from_configuration_promotes(monkeypatch) -> None:
    monkeypatch.setenv("REAPER_V2_STALE_FRACTION_THRESHOLD", "25")
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      units=UNITS, coverage=GLOBAL_FULL, stale_fraction=0.4)
    assert report["decision_class"] == "global"
    assert report["stale_fraction_threshold"] == pytest.approx(0.25)


def test_bad_stale_threshold_fails_closed(monkeypatch) -> None:
    for value in ("150", "-1", "abc"):
        monkeypatch.setenv("REAPER_V2_STALE_FRACTION_THRESHOLD", value)
        with pytest.raises(ValueError):
            stale_fraction_threshold()


# --- Stage E4: a global claim must cover the project in time, not only in sources

GLOBAL_FULL_WITH_LENGTH = dict(GLOBAL_FULL, length={
    "required": [{"start_s": 0.0, "end_s": 120.0}],
    "required_s": 120.0,
    "uncovered": [{"source_id": "track:A", "gap_count": 1,
                   "gaps": [{"start_s": 15.0, "end_s": 120.0}]}],
    "complete": False,
})

GLOBAL_FULL_TIME_OK = dict(GLOBAL_FULL, length={
    "required": [{"start_s": 0.0, "end_s": 120.0}],
    "required_s": 120.0,
    "uncovered": [],
    "complete": True,
})


def test_global_claim_on_a_short_window_is_refused_by_length() -> None:
    report = evaluate(requested_class="global", target_sources=["track:A"],
                      units=UNITS, coverage=GLOBAL_FULL_WITH_LENGTH)
    assert report["ok"] is False
    codes = [blocker["code"] for blocker in report["blockers"]]
    assert "coverage_incomplete_length" in codes
    message = next(b["message"] for b in report["blockers"]
                   if b["code"] == "coverage_incomplete_length")
    assert "120.0 s" in message
    assert "15.0-120.0 s" in message


def test_global_claim_with_full_time_coverage_passes() -> None:
    report = evaluate(requested_class="global", target_sources=["track:A"],
                      units=UNITS, coverage=GLOBAL_FULL_TIME_OK)
    assert report["ok"] is True, report["blockers"]


def test_a_narrowed_local_claim_is_not_judged_by_length() -> None:
    report = evaluate(requested_class="local", target_sources=["track:A"],
                      units=UNITS, coverage=GLOBAL_FULL_WITH_LENGTH)
    assert report["ok"] is True
    assert not any(b["code"] == "coverage_incomplete_length"
                   for b in report["blockers"])


def test_uncovered_spans_merges_touching_intervals() -> None:
    spans = uncovered_spans(
        [{"start_s": 0.0, "end_s": 6.0}, {"start_s": 6.0, "end_s": 15.0000001}],
        [{"start_s": 0.0, "end_s": 15.0}])
    assert spans == []


def test_uncovered_spans_reports_the_gap_edges() -> None:
    assert uncovered_spans([{"start_s": 2.0, "end_s": 4.0}],
                           [{"start_s": 0.0, "end_s": 10.0}]) == [
        {"start_s": 0.0, "end_s": 2.0}, {"start_s": 4.0, "end_s": 10.0}]


def test_interval_coverage_marks_missing_sources_by_name() -> None:
    passport = {"intervals": {"track:A": [{"start_s": 0.0, "end_s": 5.0}]},
                "sources": {"measured": ["track:A"]}}
    report = interval_coverage(passport, required=[{"start_s": 0.0, "end_s": 15.0}])
    assert report["complete"] is False
    assert report["uncovered"][0]["source_id"] == "track:A"
