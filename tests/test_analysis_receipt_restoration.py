"""Regression tests for accepting a render-restoration counter change only.

A capture mutes and restores tracks for its isolation renders, which advances
REAPER's change counter while the project identity stays identical.  Stored
audio evidence keeps the pre-capture counter, while the panorama receipt for
that capture is minted at the post-capture counter, so the two can never
compare equal.  These tests pin the *narrow* exception: the counter may differ
only when project identity, path, bridge instance and epoch are the same, the
receipt still belongs to that exact capture, and every other case stays stale.
"""

import pytest

from reaper_audio import mcp_server
from reaper_audio.errors import StaleContext

PROJECT = "reaper:{A15E3431-9A9E-43E6-AF7A-36231C8F0E79}:C:\\p\\v8.RPP"
PATH = "C:\\p\\v8.RPP"
INSTANCE = "6af87df4-3845e4b87a"
CAPTURE = "cap_test"
ROOT = "root-from-sealed-capture"


def _stub_root(monkeypatch, capture_root: str | None = ROOT):
    monkeypatch.setattr(mcp_server, "_capture_document_root",
                        lambda archive_root, capture_id: capture_root)


def _revision(change_count=6753, **overrides):
    value = {
        "project_id": PROJECT,
        "path": PATH,
        "instance_id": INSTANCE,
        "epoch": 10,
        "change_count": change_count,
        "capture_id": "cap_test",
    }
    value.update(overrides)
    return value


def _record(**overrides):
    record = {
        "analysis_id": "a-test",
        "project_id": PROJECT,
        "revision": _revision(6753),
        "revision_after": _revision(7050),
        "capture_id": "cap_test",
        "report": {},
    }
    record.update(overrides)
    return record


def test_counter_only_advance_is_render_restoration():
    assert mcp_server._is_render_restoration_of(_revision(6753), _revision(7050))


@pytest.mark.parametrize("overrides", [
    {"path": "C:\\p\\other.RPP"},
    {"project_id": "reaper:{OTHER}:C:\\p\\v8.RPP"},
    {"instance_id": "other-instance"},
    {"epoch": 11},
])
def test_identity_change_is_never_render_restoration(overrides):
    assert not mcp_server._is_render_restoration_of(
        _revision(6753), _revision(7050, **overrides))


@pytest.mark.parametrize("before,after", [(7050, 6753), (6753, 6753), ("x", 7050)])
def test_non_advancing_or_unparsable_counters_are_rejected(before, after):
    assert not mcp_server._is_render_restoration_of(_revision(before), _revision(after))


@pytest.mark.parametrize("missing", [{"instance_id": None}, {"instance_id": ""},
                                     {"epoch": None}, {"epoch": 0}, {"epoch": False}])
def test_incomplete_continuity_identity_cannot_prove_restoration(missing):
    """ProjectRevision treats a missing instance/epoch as no proof at all."""
    assert mcp_server._revision_identity(_revision(6753, **missing)) is None
    assert not mcp_server._is_render_restoration_of(
        _revision(6753), _revision(7050, **missing))
    assert not mcp_server._is_render_restoration_of(
        _revision(6753, **missing), _revision(7050))


def test_missing_revisions_are_rejected():
    assert not mcp_server._is_render_restoration_of(None, _revision(7050))
    assert not mcp_server._is_render_restoration_of(_revision(6753), None)


def test_receipt_is_retried_at_the_capture_revision_with_the_fact_root(monkeypatch):
    calls = []

    def fake(receipt, project_id, expected_revision, archive_root=None,
             expected_fact_root=None, *, expected_capture_id=None):
        calls.append({"count": expected_revision["change_count"],
                      "fact_root": expected_fact_root,
                      "capture": expected_capture_id})
        if expected_revision["change_count"] == 6753:
            raise StaleContext("Panorama receipt does not match the expected revision")
        return {"complete": True, "accepted_at": expected_revision["change_count"]}

    monkeypatch.setattr(mcp_server, "_require_interpretation_receipt", fake)
    _stub_root(monkeypatch)
    accepted = mcp_server._require_analysis_receipt("pr_x", _record(), "archive")

    assert accepted["accepted_at"] == 7050
    assert calls[0]["fact_root"] is None          # evidence revision keeps its own path
    assert calls[1]["fact_root"] == ROOT          # fallback binds the sealed root
    assert {call["capture"] for call in calls} == {CAPTURE}


def test_unresolvable_capture_root_fails_closed(monkeypatch):
    def fake(*args, **kwargs):  # pragma: no cover - the retry must not happen
        raise StaleContext("Panorama receipt does not match the expected revision")

    monkeypatch.setattr(mcp_server, "_require_interpretation_receipt", fake)
    _stub_root(monkeypatch, capture_root=None)

    with pytest.raises(StaleContext) as excinfo:
        mcp_server._require_analysis_receipt("pr_x", _record(), "archive")
    assert "cannot be resolved" in str(excinfo.value)


def test_same_identity_higher_counter_changed_structural_state_is_rejected(monkeypatch):
    """A receipt for another immutable document must never unlock this capture."""
    def fake(receipt, project_id, expected_revision, archive_root=None,
             expected_fact_root=None, *, expected_capture_id=None):
        if expected_revision["change_count"] == 6753:
            raise StaleContext("Panorama receipt does not match the expected revision")
        # Emulates PanoramaLifecycle._check_root: the receipt carries the root of
        # a later structural state, so a verified counter advance with identical
        # identity still stays stale.
        raise StaleContext(
            "Panorama receipt is for another fact root",
            details={"receipt_root": "root-of-a-later-edit",
                     "current_root": expected_fact_root},
        )

    monkeypatch.setattr(mcp_server, "_require_interpretation_receipt", fake)
    _stub_root(monkeypatch)

    with pytest.raises(StaleContext) as excinfo:
        mcp_server._require_analysis_receipt("pr_x", _record(), "archive")
    assert "another fact root" in str(excinfo.value)


def test_identity_change_is_not_retried(monkeypatch):
    calls = []

    def fake(receipt, project_id, expected_revision, archive_root=None,
             expected_fact_root=None, *, expected_capture_id=None):
        calls.append(dict(expected_revision))
        raise StaleContext("Panorama receipt does not match the expected revision")

    monkeypatch.setattr(mcp_server, "_require_interpretation_receipt", fake)
    record = _record(revision_after=_revision(7050, path="C:\\p\\v9.RPP"))

    with pytest.raises(StaleContext):
        mcp_server._require_analysis_receipt("pr_x", record, "archive")
    assert len(calls) == 1


def test_record_without_capture_id_is_not_retried(monkeypatch):
    calls = []

    def fake(receipt, project_id, expected_revision, archive_root=None,
             expected_fact_root=None, *, expected_capture_id=None):
        calls.append(expected_capture_id)
        raise StaleContext("Panorama receipt does not match the expected revision")

    monkeypatch.setattr(mcp_server, "_require_interpretation_receipt", fake)
    record = _record(capture_id=None)
    record["revision"] = {key: value for key, value in record["revision"].items()
                          if key != "capture_id"}

    with pytest.raises(StaleContext):
        mcp_server._require_analysis_receipt("pr_x", record, "archive")
    assert calls == [None]


def test_retry_that_also_fails_stays_closed(monkeypatch):
    def fake(receipt, project_id, expected_revision, archive_root=None,
             expected_fact_root=None, *, expected_capture_id=None):
        raise StaleContext(
            "Panorama receipt is for another panorama document",
            details={"receipt_revision": expected_revision},
        )

    monkeypatch.setattr(mcp_server, "_require_interpretation_receipt", fake)
    _stub_root(monkeypatch)

    with pytest.raises(StaleContext) as excinfo:
        mcp_server._require_analysis_receipt("pr_x", _record(), "archive")
    assert "another panorama document" in str(excinfo.value)


def test_mix_evidence_uses_the_restoration_path(monkeypatch):
    """The tool must not fall back to the raw interpretation gate."""
    record = _record(report={"sources": {}, "coverage": {}})
    monkeypatch.setattr(mcp_server, "_prune_analysis_state", lambda: None)
    monkeypatch.setitem(mcp_server._analysis_records, "a-test", {
        **record, "expires_at": 10 ** 12,
    })
    seen = {}

    def fake(receipt, record_value, archive_root=None):
        seen["capture_id"] = record_value.get("capture_id")
        return {"complete": True}

    monkeypatch.setattr(mcp_server, "_require_analysis_receipt", fake)

    result = mcp_server.reaper_mix_evidence(
        analysis_id="a-test", panorama_receipt="pr_x", archive_root="archive")

    assert result["ok"] is True
    assert seen["capture_id"] == "cap_test"
