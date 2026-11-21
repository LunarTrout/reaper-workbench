"""Tests for filling the frozen project state from a capture record.

No REAPER, no bridge and no clock: the session is one plain digest mapping and
the record is a minimal record-like object carrying the analysis reports the
capture already produced.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from reaper_audio.context.capture_state import fill_state_from_capture
from reaper_audio.context.project_state import ProjectState, StateStore

PROJECT = "reaper:{00000000-0000-4000-8000-000000000000}:/songs/song.rpp"
TRACK_A = "{AAAAAAAA-0000-4000-8000-00000000000A}"
TRACK_B = "{BBBBBBBB-0000-4000-8000-00000000000B}"
MASTER = "{MASTER00-0000-4000-8000-00000000000M}"

TRACK_STAGE = "selected_track_via_master_parallel_batch_post_fx"
MASTER_STAGE = "master_mix_post_fx"
REVISION = {"project_id": PROJECT, "change_count": 3, "path": "song.rpp"}
WINDOW = {"project_start_s": 0.0, "project_end_s": 4.0}


class _DigestSession:
    """One live project read; the adapter must not ask twice."""

    def __init__(self, digest: dict[str, Any]) -> None:
        self._digest = digest
        self.digest_calls = 0

    def digest(self, project_id: str | None = None) -> dict[str, Any]:
        self.digest_calls += 1
        return copy.deepcopy(self._digest)


def _digest_row(guid: str, *, volume: float = 1.0) -> dict[str, Any]:
    return {
        "guid": guid,
        "volume": volume,
        "pan": 0.0,
        "mute": False,
        "solo": 0,
        "channel_count": 2,
        "fx": [],
        "sends": [],
    }


def _digest(*, track_a_volume: float = 1.0) -> dict[str, Any]:
    return {
        "project_id": PROJECT,
        "path": "song.rpp",
        "change_count": 3,
        "digest_version": 1,
        "coverage": {"tracks": True, "items": True, "sends": True,
                     "automation": True, "master": True},
        "missing": [],
        "fields": {
            "tracks": [
                _digest_row(TRACK_A, volume=track_a_volume),
                _digest_row(TRACK_B, volume=0.75),
            ],
            "master": _digest_row(MASTER, volume=0.9),
            "items": [],
            "tempo": [],
            "automation": {
                "tracks": [{"track_guid": TRACK_A, "envelopes": []},
                           {"track_guid": TRACK_B, "envelopes": []}],
                "master": [],
                "items": [],
                "fx": [],
                "complete": True,
            },
        },
    }


def _report(*, lufs: float, peak: float) -> dict[str, Any]:
    """A report shaped like ``measure_audio(...).to_dict()``."""
    return {
        "sample_rate": 48000,
        "channels": 2,
        "duration_s": 4.0,
        "section": None,
        "metrics": {
            "integrated_lufs": {"value": lufs, "unit": "LUFS", "note": None},
            "peak_dbfs": {"value": peak, "unit": "dBFS", "note": None},
            "crest_db": {"value": 12.5, "unit": "dB", "note": None},
            "side_mid_db": {"value": -3.25, "unit": "dB", "note": None},
            "spectral_centroid_hz": {"value": 1234.5, "unit": "Hz", "note": None},
            "local_tempo_median_bpm": {"value": None, "unit": "BPM", "note": None},
        },
        "curves": {
            "band_centers_hz": [31.5, 63.0, 125.0],
            "band_level_dbfs": [-40.5, -34.25, -28.0],
        },
        "provenance": {
            "stage": "file_or_buffer",
            "measured_section_s": {"start_s": 0.0, "end_s": 4.0},
        },
        "coverage": {"levels": "complete"},
    }


def _audio_row(guid: str, stage: str, *, is_master: bool = False) -> dict[str, Any]:
    return {
        "source_guid": guid,
        "name": "",
        "is_master": is_master,
        "stage": stage,
        "signal_window": dict(WINDOW),
        "sha256": "0" * 64,
    }


def _record(*, analysis: dict[str, Any], audio: list[dict[str, Any]],
            sources: list[dict[str, Any]] | None = None,
            tracks: list[dict[str, Any]] | None = None,
            master: dict[str, Any] | None = None) -> SimpleNamespace:
    """The record-like object; only capture fields are required."""
    return SimpleNamespace(
        project_id=PROJECT,
        analysis=analysis,
        audio=audio,
        sources=list(sources or []),
        project_map={"tracks": list(tracks or []), "master": dict(master or {})},
        time_window=dict(WINDOW),
        revision_before=dict(REVISION),
        revision_after=dict(REVISION),
    )


def _capture(*, track_a_lufs: float = -18.25, track_a_peak: float = -6.5,
             extra_sources: list[dict[str, Any]] | None = None,
             extra_analysis: dict[str, Any] | None = None
             ) -> tuple[SimpleNamespace, dict[str, Any]]:
    analysis = {
        TRACK_A: _report(lufs=track_a_lufs, peak=track_a_peak),
        TRACK_B: _report(lufs=-21.0, peak=-9.5),
        MASTER: _report(lufs=-14.5, peak=-1.25),
    }
    analysis.update(extra_analysis or {})
    tracks = [{"guid": TRACK_A, "name": ""}, {"guid": TRACK_B, "name": ""}]
    record = _record(
        analysis=analysis,
        audio=[_audio_row(TRACK_A, TRACK_STAGE),
               _audio_row(TRACK_B, TRACK_STAGE),
               _audio_row(MASTER, MASTER_STAGE, is_master=True)],
        sources=extra_sources,
        tracks=tracks,
        master={"guid": MASTER},
    )
    return record, analysis


def _state(tmp_path: Path) -> ProjectState:
    return ProjectState(StateStore(tmp_path / "archive"), PROJECT)


def test_first_capture_upserts_objects_and_writes_exact_facts(tmp_path: Path) -> None:
    record, analysis = _capture()
    session = _DigestSession(_digest())
    state = _state(tmp_path)

    result = fill_state_from_capture(
        state, session, record, section=(0.0, 4.0), revision=REVISION)

    assert session.digest_calls == 1
    assert result["facts_written"] == 3
    assert result["state_token"] == state.state_token

    objects = state.objects
    assert set(objects["tracks"]) == {TRACK_A, TRACK_B}
    assert objects["tracks"][TRACK_A]["fingerprint"]["fields"]["volume"] == 1.0
    assert objects["master"]["fingerprint"]["guid"] == MASTER
    assert objects["routing"] is not None
    assert result["objects"]["tracks"]["added"] == sorted([TRACK_A, TRACK_B])
    assert result["objects"]["changed"] == {}

    for guid, stage in ((TRACK_A, TRACK_STAGE), (TRACK_B, TRACK_STAGE),
                        (MASTER, MASTER_STAGE)):
        evidence = f"master:{MASTER}" if guid == MASTER else f"track:{guid}"
        facts = state.facts_for(source_id=evidence)
        assert len(facts) == 1
        fact = facts[0]
        assert fact["window"] == [0.0, 4.0]
        assert fact["stage"] == stage
        assert fact["revision"] == REVISION
        expected = {
            name: cell["value"]
            for name, cell in analysis[guid]["metrics"].items()
            if cell["value"] is not None
        }
        expected["band_level_dbfs"] = analysis[guid]["curves"]["band_level_dbfs"]
        assert fact["metrics"] == expected
        assert fact["fingerprints"]["master"]["guid"] == MASTER
        assert fact["fingerprints"]["routing"] is not None
        source = fact["fingerprints"]["source"]
        assert source["guid"] == (MASTER if guid == MASTER else guid)


def test_second_capture_marks_facts_stale_before_upserting_new_ones(
        tmp_path: Path) -> None:
    record, _ = _capture()
    state = _state(tmp_path)
    fill_state_from_capture(state, _DigestSession(_digest()), record,
                            section=(0.0, 4.0), revision=REVISION)
    state.save()
    token_before = state.state_token
    old_fact = state.facts_for(source_id=f"track:{TRACK_A}")[0]

    reloaded = _state(tmp_path)
    changed_record, _ = _capture(track_a_lufs=-15.0, track_a_peak=-3.0)
    result = fill_state_from_capture(
        reloaded, _DigestSession(_digest(track_a_volume=0.5)),
        changed_record, section=(0.0, 4.0), revision=REVISION)

    assert result["changes"]["changed_objects"][TRACK_A] == ["volume"]
    assert result["changes"]["stale_facts"] == {
        old_fact["id"]: "source:volume",
    }

    log = reloaded.state["changes"]
    stale_seqs = [entry["seq"] for entry in log if entry["kind"] == "stale"]
    new_fact_seqs = [entry["seq"] for entry in log
                     if entry["kind"] == "fact" and entry["seq"] > token_before]
    assert stale_seqs
    assert new_fact_seqs
    # mark_stale ran first: every invalidation precedes the new facts.
    assert max(stale_seqs) < min(new_fact_seqs)
    assert log[[entry["seq"] for entry in log].index(stale_seqs[0])]["facts"] == {
        old_fact["id"]: "source:volume",
    }

    facts = reloaded.facts_for(source_id=f"track:{TRACK_A}")
    assert len(facts) == 2
    superseded = next(fact for fact in facts if fact["status"] == "superseded")
    current = next(fact for fact in facts if fact["status"] == "current")
    assert superseded["id"] == old_fact["id"]
    assert superseded["stale_reason"] == "source:volume"
    assert current["metrics"]["integrated_lufs"] == -15.0
    assert current["metrics"]["peak_dbfs"] == -3.0

    # A track whose source did not move keeps a current fact with its values.
    unchanged = reloaded.facts_for(source_id=f"track:{TRACK_B}",
                                   status="current")
    assert len(unchanged) == 1
    assert unchanged[0]["metrics"]["integrated_lufs"] == -21.0


def test_external_file_facts_carry_no_project_fingerprints(tmp_path: Path) -> None:
    digest_sha = "ab" * 32
    external_report = _report(lufs=-16.0, peak=-2.5)
    record = _record(
        analysis={f"file:{digest_sha}": external_report},
        audio=[],
        sources=[{
            "source_id": f"file:{digest_sha}",
            "evidence_source_id": f"file:{digest_sha}",
            "stage": "external_file",
            "kind": "file",
            "sha256": digest_sha,
            "window": {"start_s": 1.0, "end_s": 2.5},
        }],
    )
    state = _state(tmp_path)

    result = fill_state_from_capture(
        state, _DigestSession(_digest()), record,
        section=(0.0, 4.0), revision=REVISION)

    assert result["facts_written"] == 1
    facts = state.facts_for(source_id=f"file:{digest_sha}")
    assert len(facts) == 1
    fact = facts[0]
    assert fact["status"] == "current"
    assert fact["stage"] == "external_file"
    assert fact["window"] == [1.0, 2.5]
    assert fact["fingerprints"] == {"source": None, "master": None,
                                    "routing": None}
    assert fact["metrics"]["integrated_lufs"] == -16.0
    assert fact["metrics"]["band_level_dbfs"] == [-40.5, -34.25, -28.0]


def test_second_capture_does_not_invent_a_change_for_equal_rows(
        tmp_path: Path) -> None:
    record, _ = _capture()
    state = _state(tmp_path)
    fill_state_from_capture(state, _DigestSession(_digest()), record,
                            section=(0.0, 4.0), revision=REVISION)

    token_before = state.state_token
    result = fill_state_from_capture(
        state, _DigestSession(_digest()), _capture()[0],
        section=(0.0, 4.0), revision=REVISION)

    assert result["changes"]["changed"] is False
    assert result["changes"]["changed_objects"] == {}
    assert result["changes"]["stale_facts"] == {}
    assert [entry["kind"] for entry in state.state["changes"]
            if entry["seq"] > token_before] == ["fact", "fact", "fact"]
