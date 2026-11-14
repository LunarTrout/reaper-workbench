from reaper_audio.analysis.mix_evidence import build_mix_evidence


def _report(**partial_overrides):
    partial = {
        "status": "available", "node": "master", "stage": "post_fx_sum",
        "window": {"start_s": 0.0, "end_s": 2.0}, "n_sources": 2,
        "share_denominator": "sum_of_source_partial_sones",
        "equal_share_baseline_pct": 50.0, "gate": {"status": "pass"},
        "sources": {"a": {"summary": {"share_of_partial_loudness_pct": 50.0}}},
    }
    partial.update(partial_overrides)
    return {
        "sources": {
            "a": {"track_context": {"name": "guitar L"},
                  "metrics": {"rms_dbfs": {"value": -18.0},
                              "peak_dbfs": {"value": -6.0},
                              "crest_db": {"value": 12.0},
                              "integrated_lufs": {"value": -20.0}}},
            "b": {"track_context": {"name": "guitar R"},
                  "metrics": {"rms_dbfs": {"value": -19.0},
                              "peak_dbfs": {"value": -7.0},
                              "crest_db": {"value": 11.0},
                              "integrated_lufs": {"value": -21.0}}},
        },
        "masking_evidence": {"pairs": [
            {"masker": "a", "target": "b", "band_hz": 300.0, "db": 9.0}]},
        "reference_corridors": {"a": {"rows": {
            "crest_db": {"mine": 12.0, "median": 8.0, "mad": 1.0,
                         "delta": 4.0, "unit": "dB"}}}},
        "partial_loudness": partial,
        "pairwise_phase": {"pairs": {
            "a|b": {"sources": ["guitar L", "guitar R"], "correlation": 0.1}}},
        "coverage": {"interpretation": "measurements only"},
    }


def test_mix_evidence_reports_measurements_and_questions_only():
    evidence = build_mix_evidence(_report())
    ids = {issue["id"] for issue in evidence["issues"]}
    assert {"band_dominance", "reference_delta", "phase"} <= ids
    assert evidence["issues"][0]["severity"] >= 1.0
    assert all("action" not in issue for issue in evidence["issues"])
    assert evidence["source_levels"]["guitar L"]["crest_db"] == 12.0
    assert "decision" in evidence
    assert evidence["schema_version"] == 2


def test_partial_loudness_shares_are_descriptive_not_an_issue():
    evidence = build_mix_evidence(_report())

    ids = {issue["id"] for issue in evidence["issues"]}
    assert "partial_audibility" not in ids
    assert not any("too quiet" in issue.get("question", "") for issue in evidence["issues"])
    block = evidence["partial_loudness"]
    assert block["status"] == "available"
    assert block["node"] == "master"
    assert block["shares_pct"] == {"guitar L": 50.0}
    assert block["n_sources"] == 2
    assert block["equal_share_baseline_pct"] == 50.0
    assert block["gate"] == "pass"
    assert "severity" not in block
    assert "not audibility" in block["interpretation"].lower()


def test_mix_evidence_omits_partial_loudness_withheld_by_the_gate():
    evidence = build_mix_evidence(_report(status="unavailable", sources={},
                                          gate={"status": "fail"},
                                          equal_share_baseline_pct=None))

    assert evidence["partial_loudness"] is None
    assert not any(issue["id"].startswith("partial") for issue in evidence["issues"])
