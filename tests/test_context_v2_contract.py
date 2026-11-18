"""Unit tests for the Context Contract v2 modules (stages A/B/C vocabulary).

These tests never touch REAPER: they pin the rules that the wiring relies on.
"""

from __future__ import annotations

import json

import pytest

from reaper_audio.context.blockers import collect
from reaper_audio.context.citations import verify_citations
from reaper_audio.context.coverage import build_passport, passport_from_units, require_complete
from reaper_audio.context.decisions import full_pass_reasons, resolve_class
from reaper_audio.context.evidence import EvidenceStore, unit_id
from reaper_audio.context.freshness import build_freshness, states_by_source
from reaper_audio.context.measurement_cache import measurement_key
from reaper_audio.errors import ValidationError

TRACK_A = "{11111111-1111-1111-1111-111111111111}"
TRACK_B = "{22222222-2222-2222-2222-222222222222}"
MASTER = "{33333333-3333-3333-3333-333333333333}"


def _record(*, complete_a: bool = True, key_a: str = "rk1:aaa",
            key_b: str = "rk1:bbb", key_master: str = "rk1:mmm") -> dict:
    return {
        "capture_id": "cap_test",
        "revision_after": {"change_count": 5, "epoch": 1, "instance_id": "s1"},
        "project_map": {
            "render_manifest": {
                "sources": [
                    {"source_id": f"track:{TRACK_A}", "source_guid": TRACK_A,
                     "scope": "source", "stage": "selected_track_via_master_parallel_batch_post_fx",
                     "render_key": key_a, "complete": complete_a},
                    {"source_id": f"track:{TRACK_B}", "source_guid": TRACK_B,
                     "scope": "source", "stage": "selected_track_via_master_parallel_batch_post_fx",
                     "render_key": key_b, "complete": True},
                    {"source_id": f"master:{MASTER}", "source_guid": MASTER,
                     "scope": "master", "stage": "master_mix_post_fx",
                     "render_key": key_master, "complete": True},
                ],
            },
        },
    }


def test_freshness_current_when_fingerprints_match():
    current = {f"track:{TRACK_A}": {"via_master": "rk1:aaa"},
               f"track:{TRACK_B}": {"via_master": "rk1:bbb"},
               f"master:{MASTER}": {"via_master": "rk1:mmm"}}
    report = build_freshness(_record(), current_fingerprints=current)
    assert report["summary"]["sources_current"] == 3
    assert states_by_source(report)[f"track:{TRACK_A}"] == "current"


def test_freshness_one_track_edit_is_stale_with_reason():
    current = {f"track:{TRACK_A}": {"via_master": "rk1:aaa-new"},
               f"track:{TRACK_B}": {"via_master": "rk1:bbb"},
               f"master:{MASTER}": {"via_master": "rk1:mmm-new"}}
    report = build_freshness(_record(), current_fingerprints=current)
    states = states_by_source(report)
    assert states[f"track:{TRACK_A}"] == "stale"
    assert states[f"track:{TRACK_B}"] == "current"
    assert states[f"master:{MASTER}"] == "stale"
    row = report["sources"][f"track:{TRACK_A}"]["via_master"]
    assert row["reason"] == "fingerprint_changed"
    assert row["previous_fingerprint"] == "rk1:aaa"


def test_incomplete_fingerprint_is_unknown_never_clean():
    current = {f"track:{TRACK_A}": {"via_master": "rk1:aaa"},
               f"track:{TRACK_B}": {"via_master": "rk1:bbb"},
               f"master:{MASTER}": {"via_master": "rk1:mmm"}}
    report = build_freshness(_record(complete_a=False), current_fingerprints=current)
    row = report["sources"][f"track:{TRACK_A}"]["via_master"]
    assert row["state"] == "unknown"
    assert row["reason"] == "fingerprint_incomplete"


def test_unreadable_current_state_makes_everything_unknown():
    report = build_freshness(_record(), current_fingerprints=None)
    assert report["summary"]["sources_unknown"] == 3


def test_freshness_keeps_caller_intervals_untouched():
    intervals = {f"track:{TRACK_A}": [{"start_s": 9.0, "end_s": 15.0}]}
    report = build_freshness(_record(), current_fingerprints=None,
                             intervals=intervals)
    assert report["sources"][f"track:{TRACK_A}"]["via_master"]["intervals"] == [
        {"start_s": 9.0, "end_s": 15.0}]


def test_decision_class_escalates_to_global_for_master_and_many_sources():
    assert resolve_class("local", sources=[TRACK_A])["effective"] == "local"
    master = resolve_class("local", sources=[TRACK_A], touches_master=True)
    assert master["effective"] == "global" and master["raised"]
    three = resolve_class("pairwise", sources=[TRACK_A, TRACK_B, MASTER])
    assert three["effective"] == "global"
    global_declared = resolve_class("global", sources=[TRACK_A])
    assert global_declared["effective"] == "global" and not global_declared["raised"]


def test_full_pass_triggers_are_explicit(monkeypatch):
    monkeypatch.delenv("REAPER_V2_STALE_FRACTION_THRESHOLD", raising=False)
    assert full_pass_reasons(effective_class="global")["required"] is True
    assert full_pass_reasons(effective_class="local")["required"] is False
    assert full_pass_reasons(effective_class="local", is_save=True)["required"] is True
    assert full_pass_reasons(effective_class="local",
                             passport_complete=False)["required"] is True
    assert full_pass_reasons(effective_class="local",
                             stale_fraction=0.9)["required"] is False

    monkeypatch.setenv("REAPER_V2_STALE_FRACTION_THRESHOLD", "0.5")
    hot = full_pass_reasons(effective_class="local", stale_fraction=0.6)
    assert hot["required"] is True
    assert "stale_fraction_above_configured_threshold" in hot["reasons"]


def test_coverage_passport_names_gaps_and_refuses_partial():
    passport = build_passport(scope="global", measured=[f"track:{TRACK_A}"],
                              total=[f"track:{TRACK_A}", f"track:{TRACK_B}"],
                              intervals={f"track:{TRACK_A}": [{"start_s": 0.0,
                                                               "end_s": 15.0}]},
                              revisions={"change_count": 5})
    assert passport["sources_measured"] == 1
    assert passport["sources_total"] == 2
    assert passport["complete"] is False
    with pytest.raises(ValidationError):
        require_complete(passport)


def test_passport_from_units_ignores_other_revisions(tmp_path):
    store = EvidenceStore(tmp_path)
    base = {"source_id": f"track:{TRACK_A}", "interval": {"start_s": 0.0, "end_s": 15.0},
            "metric": "rms_dbfs", "method": "m", "method_version": "1",
            "stage": "via_master", "sample_rate": 44100, "channels": 2,
            "value": -20.0, "project_id": "p",
            "revision": {"change_count": 5, "epoch": 1, "instance_id": "s1"}}
    store.put_many([base])
    stale = dict(base, revision={"change_count": 4, "epoch": 1, "instance_id": "s1"})
    store.put_many([stale])
    units = store.all_units()
    passport = passport_from_units(units, scope="global", project_id="p",
                                   total_sources=[f"track:{TRACK_A}"],
                                   revision={"change_count": 5, "epoch": 1,
                                             "instance_id": "s1"})
    assert passport["sources_measured"] == 1
    assert passport["complete"] is True


def test_citations_require_stored_units_and_exact_values(tmp_path):
    store = EvidenceStore(tmp_path)
    unit = {"source_id": f"track:{TRACK_A}", "interval": {"start_s": 9.0, "end_s": 15.0},
            "metric": "rms_dbfs", "method": "m", "method_version": "1",
            "stage": "via_master", "sample_rate": 44100, "channels": 2,
            "value": -21.05989135825179, "capture_id": None}
    identifier = store.put(unit)
    good = verify_citations(store, [{"unit_id": identifier, "metric": "rms_dbfs",
                                     "value": -21.05989135825179}])
    assert good["ok"] is True
    forged = verify_citations(store, [{"unit_id": identifier, "metric": "rms_dbfs",
                                       "value": -21.0}])
    assert forged["ok"] is False
    assert forged["failed"][0]["reason"] == "value_mismatch"
    missing = verify_citations(store, [{"unit_id": "eu_missing"}])
    assert missing["ok"] is False
    assert missing["failed"][0]["reason"] == "unit_not_found"


def test_unit_id_is_content_addressed():
    unit = {"source_id": "track:x", "interval": {"start_s": 0.0, "end_s": 1.0},
            "metric": "rms_dbfs", "method": "m", "method_version": "1",
            "stage": "via_master", "sample_rate": 44100, "channels": 2,
            "value": -20.0, "capture_id": None}
    assert unit_id(unit) == unit_id(dict(unit))
    assert unit_id(unit) != unit_id(dict(unit, value=-19.0))


def test_blockers_are_capped_and_count_the_rest():
    checks = [{"blocking": True, "code": f"c{i}", "reason": "r", "remedy": "fix"}
              for i in range(5)]
    checks.append({"blocking": False, "code": "ok", "reason": "", "remedy": ""})
    result = collect(checks)
    assert result["count"] == 3
    assert result["suppressed"] == 2
    assert [b["code"] for b in result["blockers"]] == ["c0", "c1", "c2"]


def test_measurement_key_covers_every_input():
    def key(**overrides):
        values = dict(wav_sha256="a" * 64, fingerprint="rk1:x",
                      section={"start_s": 0.0, "end_s": 15.0}, sample_rate=44100,
                      channels=2, stage="via_master")
        values.update(overrides)
        return measurement_key(**values)

    base = key()
    assert base == key()
    assert base != key(stage="premaster")
    assert base != key(section={"start_s": 9.0, "end_s": 15.0})
    assert base != key(fingerprint="rk1:y")
    assert base != key(sample_rate=48000)
    # The rendered file hash is stored for verification, not for keying: the
    # same fingerprint must map to one entry regardless of byte noise.
    assert base == key(wav_sha256="b" * 64)
