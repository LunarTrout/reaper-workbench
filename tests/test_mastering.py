import pytest

from reaper_audio.analysis.mastering import plan_mastering


def _report(lufs=-20.0, true_peak=-3.0):
    return {
        "anchor_key": "master",
        "sources": {"master": {
            "track_context": {"name": "MASTER", "is_master": True},
            "metrics": {"integrated_lufs": {"value": lufs},
                        "true_peak_estimate_dbfs": {"value": true_peak},
                        "peak_dbfs": {"value": -3.0}},
        }},
    }


def test_mastering_computes_gain_and_limiting_tradeoff():
    plan = plan_mastering(_report(), target_lufs=-14.0, ceiling_dbtp=-1.0)
    assert plan["gain_db"] == 6.0
    assert plan["predicted_true_peak_dbfs"] == 3.0
    assert plan["limiter"]["needed"] is True
    assert plan["limiter"]["gain_reduction_db"] == 4.0
    assert plan["reachable_without_limiting"] is False
    assert plan["warning"]


def test_mastering_accepts_target_below_ceiling():
    plan = plan_mastering(_report(lufs=-14.0, true_peak=-2.0),
                          target_lufs=-16.0, ceiling_dbtp=-1.0)
    assert plan["gain_db"] == -2.0
    assert plan["predicted_true_peak_dbfs"] == -4.0
    assert plan["limiter"]["needed"] is False
    assert plan["warning"] is None


def test_mastering_rejects_positive_ceiling():
    with pytest.raises(ValueError):
        plan_mastering(_report(), target_lufs=-14.0, ceiling_dbtp=0.0)
