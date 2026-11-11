from reaper_audio.analysis.mix_evidence import build_mix_evidence


def test_mix_evidence_reports_measurements_and_questions_only():
    report = {
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
        "partial_loudness": {"sources": {
            "a": {"summary": {"share_of_partial_loudness_pct": 50.0}}}},
        "pairwise_phase": {"pairs": {
            "a|b": {"sources": ["guitar L", "guitar R"], "correlation": 0.1}}},
        "coverage": {"interpretation": "measurements only"},
    }
    evidence = build_mix_evidence(report)
    ids = {issue["id"] for issue in evidence["issues"]}
    assert {"band_dominance", "reference_delta", "partial_audibility",
            "phase"} <= ids
    assert evidence["issues"][0]["severity"] >= 1.0
    assert all("action" not in issue for issue in evidence["issues"])
    assert evidence["source_levels"]["guitar L"]["crest_db"] == 12.0
    assert "decision" in evidence
