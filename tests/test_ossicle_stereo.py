"""Stereo-path tests for the Ossicle-4B backend (no GPU, no weights, no inference).

These prove the plumbing keeps real left/right channels and that the request the worker
receives carries them as separate clips. Nothing here loads the model.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.analysis import ossicle as os_

SR = 16000


def _write(path, left, right, sr=SR):
    sf.write(str(path), np.stack([left, right], axis=1), sr, subtype="FLOAT")
    return path


def _sine(freq, seconds, amp=0.5, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_hard_left_is_preserved_as_two_channels(tmp_path):
    left = _sine(440, 2.0)
    right = np.zeros_like(left)
    wav = _write(tmp_path / "left.wav", left, right)

    x, sr = os_.load_binaural(wav)
    assert sr == SR
    assert x.shape == (2, 2 * SR), "channels must stay first and separate"
    energy = [float(np.sum(ch ** 2)) for ch in x]
    assert energy[0] > 1e3 * max(energy[1], 1e-12), "left channel must not leak into right"


def test_swap_mirrors_the_scene(tmp_path):
    left = _sine(300, 2.0, 0.6)
    right = _sine(900, 2.0, 0.2)
    a = os_.stereo_facts(os_.load_binaural(_write(tmp_path / "a.wav", left, right))[0])
    b = os_.stereo_facts(os_.load_binaural(_write(tmp_path / "b.wav", right, left))[0])
    assert a["balance_db"] == pytest.approx(-b["balance_db"], abs=0.01)
    assert a["correlation"] == pytest.approx(b["correlation"], abs=1e-6)


def test_centered_source_stays_centered(tmp_path):
    mono = _sine(500, 2.0, 0.4)
    facts = os_.stereo_facts(os_.load_binaural(_write(tmp_path / "c.wav", mono, mono))[0])
    assert facts["correlation"] == pytest.approx(1.0, abs=1e-4)
    assert facts["balance_db"] == pytest.approx(0.0, abs=0.01)


def test_mono_source_is_refused_by_default(tmp_path):
    path = tmp_path / "mono.wav"
    sf.write(str(path), _sine(200, 2.0), SR, subtype="FLOAT")
    with pytest.raises(os_.OssicleError) as exc:
        os_.load_binaural(path)
    assert exc.value.code == "NOT_STEREO"


def test_allow_mono_duplicates_instead_of_dropping_a_channel(tmp_path):
    path = tmp_path / "mono.wav"
    mono = _sine(200, 2.0)
    sf.write(str(path), mono, SR, subtype="FLOAT")
    x, _ = os_.load_binaural(path, allow_mono=True)
    assert x.shape[0] == 2
    assert float(np.max(np.abs(x[0] - x[1]))) == 0.0


def test_clip_longer_than_the_stereo_ceiling_is_refused(tmp_path):
    long = _sine(220, os_.MAX_S + 2.0, 0.2)
    wav = _write(tmp_path / "long.wav", long, long)
    with pytest.raises(os_.OssicleError) as exc:
        os_.load_binaural(wav)
    assert exc.value.code == "TOO_LONG"


def test_compare_sends_two_clips_and_attaches_both_fact_sets(tmp_path, monkeypatch):
    left = _sine(300, 2.0, 0.6)
    right = _sine(900, 2.0, 0.2)
    a = _write(tmp_path / "a.wav", left, right)
    b = _write(tmp_path / "b.wav", right, left)

    captured = {}

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"ok": True, "device": "Tesla V100", "dtype": "float16",
                             "vram_peak_mb": 12000, "load_seconds": 40,
                             "answers": [{"audios": ["x", "y"], "question": "q",
                                          "text": "A is wider", "seconds": 3.0}]})
        stderr = ""

    def fake_run(cmd, input=None, **kwargs):
        captured["payload"] = json.loads(input)
        return FakeProc()

    monkeypatch.setattr(os_.subprocess, "run", fake_run)
    monkeypatch.setattr(os_, "require_gpu", lambda: {"device": {"name": "Tesla V100"}})
    monkeypatch.setattr(os_, "verify_weights", lambda: {"ok": True, "incomplete": []})

    report = os_.compare(a, b, "Which is wider?")

    jobs = captured["payload"]["jobs"]
    assert len(jobs) == 1 and len(jobs[0]["audios"]) == 2
    assert jobs[0]["question"] == "Which is wider?"
    facts = report["answers"][0]["stereo_facts"]
    assert isinstance(facts, list) and len(facts) == 2
    assert facts[0]["balance_db"] == pytest.approx(-facts[1]["balance_db"], abs=0.01)
