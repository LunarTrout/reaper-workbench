"""Focused tests for the partial-loudness reconstruction gate.

Synthetic buffers only: no REAPER project, no rendered stems, no network or
model download. Every case is deterministic through the seeded RNG below.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from reaper_audio.analysis.partial import attribute_partial_loudness
from reaper_audio.models import AudioBuffer, Section

SAMPLE_RATE = 48_000
HOP_S = 0.1
STAGE = "post_fx_sum"


def _noise(seed: int, *, amplitude: float = 0.05,
           duration_s: float = 2.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    frames = int(round(SAMPLE_RATE * duration_s))
    return rng.standard_normal((frames, 2)) * amplitude


def _buffer(samples: np.ndarray, source: str, start_s: float = 0.0) -> AudioBuffer:
    return AudioBuffer(samples, SAMPLE_RATE, source=source, start_s=start_s)


def _provenance(*, stage: str = STAGE, channel_view: str = "stereo",
                window: dict | None = None,
                nodes: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    nodes = nodes or {"a": "track:A", "b": "track:B"}
    declarations: dict[str, dict[str, Any]] = {}
    for source_id, node in nodes.items():
        entry: dict[str, Any] = {"node": node, "stage": stage,
                                 "channel_view": channel_view}
        if window is not None:
            entry["window"] = window
        declarations[source_id] = entry
    return declarations


def _attribute(mix: np.ndarray, stems: dict[str, np.ndarray], *,
               section: Section | None = None, **overrides: Any) -> dict:
    options: dict[str, Any] = {
        "node": "master", "stage": STAGE, "channel_view": "stereo",
        "source_provenance": _provenance(), "hop_s": HOP_S,
    }
    options.update(overrides)
    return attribute_partial_loudness(
        _buffer(mix, "mix"), {key: _buffer(value, key) for key, value in stems.items()},
        section=section, **options,
    )


def _all_keys(payload: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            keys.add(str(key))
            keys |= _all_keys(value)
    elif isinstance(payload, list):
        for value in payload:
            keys |= _all_keys(value)
    return keys


def _assert_no_withheld_values(payload: dict) -> None:
    assert payload["sources"] == {}
    keys = _all_keys(payload)
    assert "share_of_partial_loudness_pct" not in keys
    assert "partial_to_solo_ratio" not in keys
    assert not any("audibility" in key for key in keys)
    assert not any(key in {"too_quiet", "exposed", "quality", "verdict"} for key in keys)
    assert payload["coverage"]["shares_emitted"] is False
    json.dumps(payload, allow_nan=False)


def test_exact_linear_reconstruction_passes_and_reports_the_gate() -> None:
    a, b = _noise(1), _noise(2)

    result = _attribute(a + b, {"a": a, "b": b})

    assert result["status"] == "available"
    assert result["reason"] is None
    gate = result["gate"]
    assert gate["status"] == "pass"
    assert gate["reasons"] == []
    assert gate["result"]["median_abs_db"] == pytest.approx(0.0, abs=1e-9)
    assert gate["result"]["p95_abs_db"] == pytest.approx(0.0, abs=1e-9)
    assert gate["result"]["cells_evaluated"] == 29 * 20
    assert gate["alignment"] == {
        "status": "ok", "sample_rate": SAMPLE_RATE, "channels": 2,
        "frames": 2 * SAMPLE_RATE, "timeline_start_s": 0.0,
        "window": {"start_s": 0.0, "end_s": 2.0},
        "checks": ["same_sample_rate", "same_timeline_start", "same_frame_count",
                   "same_channel_count"],
        "problems": [],
    }
    assert gate["declaration"]["node"] == "master"
    assert gate["declaration"]["mix_source"] == "mix"
    assert gate["declaration"]["stage"] == STAGE
    assert gate["declaration"]["channel_view"] == "stereo"
    assert gate["declaration"]["sources"]["a"]["node"] == "track:A"
    assert gate["declaration"]["sources"]["b"]["node"] == "track:B"
    assert gate["tolerances_db"] == {"median_abs": 1.0, "p95_abs": 3.0, "unit": "dB"}
    assert set(result["sources"]) == {"a", "b"}
    assert result["n_sources"] == 2
    assert result["equal_share_baseline_pct"] == pytest.approx(50.0)
    assert result["share_denominator"] == "sum_of_source_partial_sones"
    assert sum(item["share_of_partial_loudness_pct"]
               for item in result["sources"].values()) == pytest.approx(100.0)
    assert "audibility" not in " ".join(_all_keys(result))
    json.dumps(result, allow_nan=False)


def test_median_error_just_over_the_bound_fails_closed() -> None:
    a, b = _noise(3), _noise(4)

    # +1.36 dB on every band, i.e. just over the 1 dB median bound while the
    # 3 dB p95 bound is still satisfied.
    result = _attribute((a + b) * 1.17, {"a": a, "b": b})

    assert result["status"] == "unavailable"
    assert result["reason"] == "reconstruction_gate_failed"
    assert result["gate"]["status"] == "fail"
    measured = result["gate"]["result"]
    assert measured["median_abs_db"] > 1.0
    assert measured["median_abs_db"] < 2.0
    assert measured["p95_abs_db"] < 3.0
    _assert_no_withheld_values(result)


def _boost_bands(samples: np.ndarray, centers: tuple[float, ...],
                 gain: float) -> np.ndarray:
    """Multiply the given third-octave bands by a linear gain (zero phase).

    Used to build a reconstruction error of a known size in a known subset of
    cells, so the p95 bound can be probed just above its value.
    """
    spectrum = np.fft.rfft(samples, axis=0)
    frequencies = np.fft.rfftfreq(samples.shape[0], 1.0 / SAMPLE_RATE)
    for center in centers:
        mask = ((frequencies >= center / 2 ** (1 / 6))
                & (frequencies < center * 2 ** (1 / 6)))
        spectrum[mask] *= gain
    return np.fft.irfft(spectrum, n=samples.shape[0], axis=0)


def test_p95_error_just_over_the_bound_fails_closed() -> None:
    a, b = _noise(5), _noise(6)
    # +3.2 dB in two of the 29 bands (6.9 % of cells), so the median stays at
    # zero and only the p95 bound is exceeded.
    mix = _boost_bands(a + b, (800.0, 1000.0), 1.45)

    result = _attribute(mix, {"a": a, "b": b})

    assert result["status"] == "unavailable"
    assert result["reason"] == "reconstruction_gate_failed"
    measured = result["gate"]["result"]
    assert measured["median_abs_db"] < 1.0
    assert 3.0 < measured["p95_abs_db"] < 4.0
    _assert_no_withheld_values(result)


def test_stage_label_mismatch_fails_closed() -> None:
    a, b = _noise(7), _noise(8)

    result = _attribute(a + b, {"a": a, "b": b},
                        source_provenance=_provenance(stage="track_pre_fx"))

    assert result["status"] == "unavailable"
    assert result["reason"] == "stage_mismatch"
    assert result["gate"]["status"] == "fail"
    # The residual is still reported so the caller can see what was measured.
    assert result["gate"]["result"]["median_abs_db"] == pytest.approx(0.0, abs=1e-9)
    _assert_no_withheld_values(result)


def test_node_reused_by_the_mix_fails_closed() -> None:
    a, b = _noise(9), _noise(10)

    result = _attribute(a + b, {"a": a, "b": b},
                        source_provenance=_provenance(nodes={"a": "master",
                                                             "b": "track:B"}))

    assert result["status"] == "unavailable"
    assert result["reason"] == "node_mismatch"
    _assert_no_withheld_values(result)


def test_two_sources_from_one_node_fail_closed() -> None:
    a, b = _noise(11), _noise(12)

    result = _attribute(a + b, {"a": a, "b": b},
                        source_provenance=_provenance(nodes={"a": "track:X",
                                                             "b": "track:X"}))

    assert result["status"] == "unavailable"
    assert result["reason"] == "node_mismatch"
    _assert_no_withheld_values(result)


def test_window_mismatch_fails_closed() -> None:
    a, b = _noise(13), _noise(14)

    result = _attribute(a + b, {"a": a, "b": b},
                        source_provenance=_provenance(window={"start_s": 0.0,
                                                              "end_s": 1.5}))

    assert result["status"] == "unavailable"
    assert result["reason"] == "window_mismatch"
    _assert_no_withheld_values(result)


def test_declared_window_must_match_the_measured_section() -> None:
    a, b = _noise(15), _noise(16)
    section = Section(0.5, 1.5)

    matching = _attribute(a + b, {"a": a, "b": b}, section=section,
                          source_provenance=_provenance(
                              window={"start_s": 0.5, "end_s": 1.5}))
    assert matching["status"] == "available"
    assert matching["gate"]["alignment"]["window"] == {"start_s": 0.5, "end_s": 1.5}

    whole_buffer = _attribute(a + b, {"a": a, "b": b}, section=section,
                              source_provenance=_provenance(
                                  window={"start_s": 0.0, "end_s": 2.0}))
    assert whole_buffer["status"] == "unavailable"
    assert whole_buffer["reason"] == "window_mismatch"
    _assert_no_withheld_values(whole_buffer)


def test_missing_provenance_fails_closed() -> None:
    a, b = _noise(17), _noise(18)

    result = attribute_partial_loudness(_buffer(a + b, "mix"),
                                        {"a": _buffer(a, "a"), "b": _buffer(b, "b")},
                                        hop_s=HOP_S)

    assert result["status"] == "unavailable"
    assert result["reason"] == "missing_provenance"
    assert result["gate"]["declaration"]["node"] is None
    _assert_no_withheld_values(result)


def test_one_undeclared_source_fails_closed() -> None:
    a, b = _noise(19), _noise(20)

    result = _attribute(a + b, {"a": a, "b": b},
                        source_provenance={"a": {"node": "track:A", "stage": STAGE,
                                                 "channel_view": "stereo"}})

    assert result["status"] == "unavailable"
    assert result["reason"] == "missing_provenance"
    _assert_no_withheld_values(result)


def test_sample_timeline_and_channel_mismatches_fail_closed() -> None:
    a, b = _noise(21), _noise(22)

    shifted = attribute_partial_loudness(
        _buffer(a + b, "mix"),
        {"a": _buffer(a, "a", start_s=0.1), "b": _buffer(b, "b")},
        node="master", stage=STAGE, channel_view="stereo",
        source_provenance=_provenance(), hop_s=HOP_S,
    )
    assert shifted["status"] == "unavailable"
    assert shifted["reason"] == "timeline_mismatch"
    _assert_no_withheld_values(shifted)

    mono = attribute_partial_loudness(
        _buffer(a + b, "mix"),
        {"a": _buffer(a[:, :1], "a"), "b": _buffer(b[:, :1], "b")},
        node="master", stage=STAGE, channel_view="stereo",
        source_provenance=_provenance(), hop_s=HOP_S,
    )
    assert mono["status"] == "unavailable"
    assert mono["reason"] == "channel_count_mismatch"
    _assert_no_withheld_values(mono)


def test_channel_view_label_mismatch_fails_closed() -> None:
    a, b = _noise(23), _noise(24)

    result = _attribute(a + b, {"a": a, "b": b},
                        source_provenance=_provenance(channel_view="mono_sum"))

    assert result["status"] == "unavailable"
    assert result["reason"] == "channel_view_mismatch"
    _assert_no_withheld_values(result)


def test_correlated_and_phase_related_renders_pass_reconstruction() -> None:
    rng = np.random.default_rng(25)
    a = _noise(26)
    b = -0.8 * a + rng.standard_normal(a.shape) * 0.005

    result = _attribute(a + b, {"a": a, "b": b})

    # Waveform reconstruction is exact even when the renders are anti-correlated,
    # so the gate admits them...
    assert result["status"] == "available"
    assert result["gate"]["result"]["median_abs_db"] == pytest.approx(0.0, abs=1e-9)
    # ...and the incoherent power sum the subtraction assumes is reported as far
    # from zero, descriptively, without becoming a second threshold.
    additivity = result["gate"]["result"]["incoherent_power_sum"]
    assert additivity["median_abs_db"] > 6.0
    assert "not thresholded" in additivity["note"]
    assert any("incoherently" in line for line in result["limitations"])
    json.dumps(result, allow_nan=False)


def test_nonlinear_master_style_mismatch_fails_closed() -> None:
    a, b = _noise(27), _noise(28)

    # A saturated sum is what a limited master looks like: the same sources, a
    # node render that no linear sum of them can reproduce.
    result = _attribute(np.tanh(4.0 * (a + b)), {"a": a, "b": b})

    assert result["status"] == "unavailable"
    assert result["reason"] == "reconstruction_gate_failed"
    assert result["gate"]["status"] == "fail"
    assert result["gate"]["result"]["median_abs_db"] > 3.0
    _assert_no_withheld_values(result)


def test_a_missing_source_fails_closed() -> None:
    a, b = _noise(31), _noise(32)

    # The declared mix contains b, but only a was rendered.
    result = _attribute(a + b, {"a": a},
                        source_provenance=_provenance(nodes={"a": "track:A"}))

    assert result["status"] == "unavailable"
    assert result["reason"] == "reconstruction_gate_failed"
    assert result["gate"]["result"]["median_abs_db"] > 1.0
    _assert_no_withheld_values(result)


def test_iso532_1_uses_its_own_band_set_and_passes_exact_reconstruction() -> None:
    a, b = _noise(33), _noise(34)

    result = _attribute(a + b, {"a": a, "b": b}, model="iso532-1")

    assert result["status"] == "available"
    assert result["gate"]["result"]["bands"] == 28
    assert result["gate"]["result"]["median_abs_db"] == pytest.approx(0.0, abs=1e-9)
    assert set(result["gate"]["result"]["per_band_median_abs_db"]) == {
        "25", "31.5", "40", "50", "63", "80", "100", "125", "160", "200", "250",
        "315", "400", "500", "630", "800", "1000", "1250", "1600", "2000",
        "2500", "3150", "4000", "5000", "6300", "8000", "10000", "12500",
    }


def test_gate_tolerances_are_declared_and_overridable() -> None:
    a, b = _noise(29), _noise(30)

    default = _attribute((a + b) * 1.17, {"a": a, "b": b})
    assert default["status"] == "unavailable"

    relaxed = _attribute((a + b) * 1.17, {"a": a, "b": b},
                         max_median_error_db=2.0, max_p95_error_db=3.0)
    assert relaxed["status"] == "available"
    assert relaxed["gate"]["tolerances_db"]["median_abs"] == 2.0
