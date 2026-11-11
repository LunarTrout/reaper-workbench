"""Corpus manifest: stages, sources and validation."""

from __future__ import annotations

import json

import numpy as np
import soundfile as sf

from reaper_audio.corpus.manifest import Entry, Manifest


def _wav(path, seconds=1.0):
    rng = np.random.default_rng(0)
    sf.write(str(path), rng.normal(0, 0.05, size=(int(32000 * seconds), 2)), 32000)


def test_scan_groups_by_source(tmp_path):
    for name in ("grima_01.wav", "grima_02.wav", "windir_01.wav"):
        _wav(tmp_path / name)
    manifest = Manifest.scan(tmp_path)
    assert len(manifest.entries) == 3
    assert set(manifest.by_source()) == {"grima", "windir"}
    assert manifest.validate() == []


def test_unknown_stage_is_flagged(tmp_path):
    _wav(tmp_path / "a_01.wav")
    manifest = Manifest.scan(tmp_path, stage="master")
    manifest.entries[0].stage = "bogus"
    problems = manifest.validate()
    assert any("unknown stage" in p for p in problems)


def test_duplicate_entry_is_flagged(tmp_path):
    _wav(tmp_path / "a_01.wav")
    manifest = Manifest.scan(tmp_path)
    manifest.entries.append(manifest.entries[0])
    assert any("duplicate" in p for p in manifest.validate())


def test_missing_file_is_flagged(tmp_path):
    manifest = Manifest([Entry(file=str(tmp_path / "nope.wav"), source="x")])
    assert any("missing" in p for p in manifest.validate())


def test_round_trip(tmp_path):
    _wav(tmp_path / "a_01.wav")
    manifest = Manifest.scan(tmp_path, stage="master")
    path = tmp_path / "manifest.json"
    manifest.save(path)
    loaded = Manifest.load(path)
    assert loaded.to_dict() == manifest.to_dict()
    assert json.loads(path.read_text())["entries"][0]["stage"] == "master"
