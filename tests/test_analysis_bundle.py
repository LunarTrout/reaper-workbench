from __future__ import annotations

import numpy as np
import pytest

from reaper_audio.analysis.bundle import analyze_bundle
from reaper_audio.errors import ValidationError
from reaper_audio.models import AudioBuffer, Section


def _tone(source: str, frequency: float, sample_rate: int = 8_000,
          amplitude: float = 0.2, start_s: float = 0.0) -> AudioBuffer:
    time = np.arange(sample_rate, dtype=np.float64) / sample_rate
    samples = amplitude * np.sin(2 * np.pi * frequency * time)
    return AudioBuffer(samples=samples, sample_rate=sample_rate, source=source,
                       start_s=start_s)


def test_bundle_runs_full_measurement_and_reference_comparison_once() -> None:
    result = analyze_bundle(
        {"track-a": _tone("track-a", 220), "master": _tone("master", 220)},
        references={"reference-1": _tone("reference-1", 330)},
        anchor_key="master",
        mix_key="master",
        include_pairwise=True,
    )

    assert set(result["sources"]) == {"track-a", "master"}
    assert set(result["references"]) == {"reference-1"}
    assert "peak_dbfs" in result["sources"]["track-a"]["metrics"]
    assert set(result["reference_comparisons"]["track-a"]) == {"reference-1"}
    assert result["anchor_comparisons"]["track-a"]["a"] == "master"
    assert len(result["pairwise_comparisons"]) == 1
    assert "masking_evidence" in result
    assert result["masking_evidence"]["interpretation"].startswith(
        "Relative band-level dominance heuristic"
    )
    timeline = result["cross_source_band_timeline"]
    assert timeline["coverage"]["status"] == "available"
    assert timeline["coverage"]["comparable_ticks"] > 0
    assert len(timeline["cells"][0]["bands"]) == 28
    assert result["coverage"]["partial_loudness"] == "available"
    assert set(result["partial_loudness"]["sources"]) == {"track-a"}
    tape_cell = result["sources"]["track-a"]["curves"]["temporal_profile"]["cells"][0]
    assert "partial_loudness" in tape_cell
    assert set(tape_cell["partial_loudness"]["regions"]) == {
        "sub", "bass", "lowmid", "mid", "upmid", "pres", "air",
    }


def test_bundle_marks_missing_reference_and_checks_requested_anchor() -> None:
    result = analyze_bundle({"one": _tone("one", 220)})

    assert result["coverage"]["references_analyzed"] == 0
    assert result["coverage"]["reference_comparison"].startswith("not_requested")
    with pytest.raises(ValidationError):
        analyze_bundle({"one": _tone("one", 220)}, anchor_key="missing")


def test_project_section_and_reference_window_use_independent_timelines() -> None:
    sample_rate = 8_000
    time = np.arange(2 * sample_rate, dtype=np.float64) / sample_rate
    reference = AudioBuffer(
        0.1 * np.sin(2 * np.pi * 330 * time), sample_rate, source="reference",
    )
    project_window = Section(40.0, 41.0)
    reference_window = Section(1.0, 2.0)

    result = analyze_bundle(
        {"mix": _tone("mix", 220, start_s=40.0)},
        references={"reference": reference},
        section=project_window,
        reference_sections={"reference": reference_window},
    )

    assert result["sources"]["mix"]["section"]["start_s"] == 40.0
    assert result["references"]["reference"]["section"]["start_s"] == 1.0


def test_project_section_does_not_implicitly_slice_full_reference() -> None:
    sample_rate = 8_000
    time = np.arange(2 * sample_rate, dtype=np.float64) / sample_rate
    reference = AudioBuffer(
        0.1 * np.sin(2 * np.pi * 330 * time), sample_rate,
        source="reference",
    )

    result = analyze_bundle(
        {"mix": _tone("mix", 220, start_s=40.0)},
        references={"reference": reference},
        section=Section(40.0, 41.0),
    )

    assert result["sources"]["mix"]["duration_s"] == pytest.approx(1.0)
    assert result["references"]["reference"]["duration_s"] == pytest.approx(2.0)


def test_bundle_adds_per_time_cross_source_band_context() -> None:
    result = analyze_bundle({
        "quiet": _tone("quiet", 220, amplitude=0.1),
        "loud": _tone("loud", 220, amplitude=0.4),
    })

    point = result["cross_source_band_timeline"]["cells"][0]
    assert point["available_sources"] == ["loud", "quiet"]
    strongest = max(point["bands"], key=lambda band: band["margin_db"])
    assert strongest["highest"]["source_id"] == "loud"
    assert strongest["margin_db"] > 10.0


def test_cross_source_timeline_reports_unaligned_ranges_without_interpolating() -> None:
    result = analyze_bundle({
        "first": _tone("first", 220, start_s=0.0),
        "offset": _tone("offset", 220, start_s=0.025),
    })

    assert result["cross_source_band_timeline"]["coverage"]["status"] == (
        "insufficient_overlap"
    )
    assert result["cross_source_band_timeline"]["coverage"]["comparable_ticks"] == 0


def test_bundle_can_request_selected_or_all_pairwise_phase_checks() -> None:
    sources = {
        "a": _tone("a", 220),
        "b": _tone("b", 220, amplitude=0.2),
        "c": _tone("c", 330),
    }

    selected = analyze_bundle(sources, phase_pairs=[("a", "b"), ("b", "a")])
    assert len(selected["pairwise_phase"]) == 1
    assert selected["coverage"]["pairwise_phase"] == "available"

    all_pairs = analyze_bundle(sources, phase_pairs="all")
    assert len(all_pairs["pairwise_phase"]) == 3
    with pytest.raises(ValidationError):
        analyze_bundle(sources, phase_pairs=[("a", "missing")])
    with pytest.raises(ValidationError):
        analyze_bundle(sources, phase_max_lag_ms=float("nan"))
