"""Tests for the append-only decision journal.

No REAPER, no bridge, no network.  Every journal lives under ``tmp_path`` and
every project id / GUID below is synthetic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reaper_audio.context import journal as journal_module
from reaper_audio.context.journal import (
    BASIS_KINDS,
    JOURNAL_FILENAME,
    RAW_AUDIO_KEYS,
    SECRET_KEYS,
    JournalRequired,
    JournalStore,
    make_entry,
)
from reaper_audio.context.project_state import safe_project_key
from reaper_audio.errors import ValidationError

PROJECT = "reaper:{11111111-0000-4000-8000-000000000001}:/songs/journal-a.rpp"
OTHER_PROJECT = "reaper:{22222222-0000-4000-8000-000000000002}:/songs/journal-b.rpp"

TRACK_A = "{AAAAAAAA-0000-4000-8000-00000000000A}"
TRACK_B = "{BBBBBBBB-0000-4000-8000-00000000000B}"
MASTER = "{MASTER00-0000-4000-8000-00000000000M}"

ENTRY_FIELDS = {
    "seq", "at", "project_id", "kind", "receipt", "why", "basis", "expected",
    "operations", "readback", "verify", "revision_before", "revision_after",
    "fx_edits", "undoes",
}


def operation(kind: str = "track.set", target: str = TRACK_A,
              **values: object) -> dict[str, object]:
    return {"kind": kind, "target_guid": target,
            "values": values or {"volume_db": -2.0}}


def apply_entry(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "project_id": PROJECT,
        "receipt": "op_0001",
        "operations": [operation()],
        "why": "tame the lead so the vocal sits forward",
    }
    payload.update(overrides)
    return make_entry(**payload)


# --------------------------------------------------------------------------- #
# round trip and layout
# --------------------------------------------------------------------------- #


def test_append_read_round_trip(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    entry = apply_entry(
        basis=[
            {"kind": "measurement", "id": "fact_0123"},
            {"kind": "fact", "id": "key_sha256:abc"},
            {"kind": "receipt", "id": "op_0000"},
            {"kind": "state", "id": "token:12"},
        ],
        expected={"volume_db": -2.0},
        readback={"matched": True,
                  "operations": [{"kind": "track.set", "matched": True}]},
        verify={"mode": "measurement", "matched": True},
        revision_before={"token": 10, "revision": "rev-a"},
        revision_after={"token": 11, "revision": "rev-b"},
        fx_edits=[{"track_guid": TRACK_A, "fx_guid": "{FX000001}",
                   "parameter": 0, "before": 0.5, "after": 0.4}],
    )
    assert "seq" not in entry

    stored = store.append(entry)

    assert stored["seq"] == 1
    assert set(stored) == ENTRY_FIELDS
    assert stored["kind"] == "apply"
    assert stored["undoes"] is None
    assert stored["receipt"] == "op_0001"
    assert stored["why"] == "tame the lead so the vocal sits forward"
    assert stored["basis"][0] == {"kind": "measurement", "id": "fact_0123"}
    assert stored["revision_after"] == {"token": 11, "revision": "rev-b"}

    parsed_at = datetime.fromisoformat(stored["at"].replace("Z", "+00:00"))
    assert parsed_at.tzinfo is not None
    assert parsed_at.utcoffset() == timezone.utc.utcoffset(parsed_at)
    assert stored["at"].endswith("Z")
    assert stored["at"].count(":") == 2

    result = store.read()
    assert set(result) == {"entries", "next_seq", "torn_line"}
    assert result["entries"] == [stored]
    assert result["next_seq"] == 1
    assert result["torn_line"] is False

    path = store.path_for(PROJECT)
    assert path == (tmp_path / "projects" / safe_project_key(PROJECT)
                    / JOURNAL_FILENAME)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["seq"] == 1


def test_seq_is_monotonic_and_per_project(tmp_path: Path) -> None:
    first = JournalStore(tmp_path, PROJECT)
    second = JournalStore(tmp_path, OTHER_PROJECT)

    assert [first.append(apply_entry())["seq"] for _ in range(3)] == [1, 2, 3]
    assert [second.append(apply_entry(project_id=OTHER_PROJECT))["seq"]
            for _ in range(2)] == [1, 2]

    assert [entry["seq"] for entry in first.read()["entries"]] == [1, 2, 3]
    assert [entry["seq"] for entry in second.read()["entries"]] == [1, 2]
    assert first.path_for(PROJECT) != second.path_for(OTHER_PROJECT)

    # An unbound store still derives the project from each entry.
    unbound = JournalStore(tmp_path)
    assert unbound.append(apply_entry())["seq"] == 4
    assert unbound.append(apply_entry(project_id=OTHER_PROJECT))["seq"] == 3
    assert unbound.read(project_id=PROJECT)["next_seq"] == 4
    assert unbound.read(project_id=OTHER_PROJECT)["next_seq"] == 3


# --------------------------------------------------------------------------- #
# filters and cursors
# --------------------------------------------------------------------------- #


def test_filter_by_receipt(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    store.append(apply_entry(receipt="op_0001"))
    store.append(apply_entry(receipt="op_0002"))
    store.append(apply_entry(receipt="op_0001"))

    result = store.read(receipt="op_0001")
    assert [entry["seq"] for entry in result["entries"]] == [1, 3]
    assert all(entry["receipt"] == "op_0001" for entry in result["entries"])
    assert result["next_seq"] == 3

    assert store.read(receipt="op_missing")["entries"] == []


def test_filter_by_track_from_operation_targets(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    store.append(apply_entry(operations=[operation(target=TRACK_A)]))
    store.append(apply_entry(operations=[operation(target=TRACK_B)]))
    store.append(apply_entry(operations=[operation(target=TRACK_A),
                                         operation(target=TRACK_B)]))
    store.append(apply_entry(operations=[operation(kind="track.set",
                                                   target=MASTER)]))

    assert [entry["seq"] for entry in store.read(track=TRACK_A)["entries"]] == [1, 3]
    assert [entry["seq"] for entry in store.read(track=TRACK_B)["entries"]] == [2, 3]
    assert [entry["seq"] for entry in store.read(track=MASTER)["entries"]] == [4]
    assert store.read(track="{UNKNOWN}")["entries"] == []


def test_since_seq_cursor(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    for _ in range(4):
        store.append(apply_entry())

    result = store.read(since_seq=2)
    assert [entry["seq"] for entry in result["entries"]] == [3, 4]
    assert result["next_seq"] == 4

    empty = store.read(since_seq=4)
    assert empty["entries"] == []
    assert empty["next_seq"] == 4

    with pytest.raises(ValidationError):
        store.read(since_seq=-1)
    with pytest.raises(ValidationError):
        store.read(since_seq=True)


def test_limit_paginates_without_gaps(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    for _ in range(5):
        store.append(apply_entry())

    first_page = store.read(limit=2)
    assert [entry["seq"] for entry in first_page["entries"]] == [1, 2]
    assert first_page["next_seq"] == 2

    second_page = store.read(since_seq=first_page["next_seq"], limit=2)
    assert [entry["seq"] for entry in second_page["entries"]] == [3, 4]
    assert second_page["next_seq"] == 4

    third_page = store.read(since_seq=second_page["next_seq"], limit=2)
    assert [entry["seq"] for entry in third_page["entries"]] == [5]
    assert third_page["next_seq"] == 5

    assert store.read(since_seq=5, limit=2)["entries"] == []
    assert store.read(limit=2, receipt="op_0001")["next_seq"] == 2

    with pytest.raises(ValidationError):
        store.read(limit=0)
    with pytest.raises(ValidationError):
        store.read(limit=-3)
    with pytest.raises(ValidationError):
        store.read(limit=True)


# --------------------------------------------------------------------------- #
# mandatory why
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_why", [None, "", "   ", "\n\t", 17, ["because"]])
def test_missing_why_is_journal_required(tmp_path: Path, bad_why: object) -> None:
    with pytest.raises(JournalRequired) as caught:
        apply_entry(why=bad_why)
    assert isinstance(caught.value, ValidationError)
    assert caught.value.code == "journal_required"
    assert caught.value.details.get("field") == "why"

    store = JournalStore(tmp_path, PROJECT)
    entry = apply_entry()
    entry.pop("why")
    with pytest.raises(JournalRequired):
        store.append(entry)
    assert not store.path_for(PROJECT).exists()


def test_why_is_stripped_but_kept(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    stored = store.append(apply_entry(why="  the second verse needs air  "))
    assert stored["why"] == "the second verse needs air"


# --------------------------------------------------------------------------- #
# torn lines
# --------------------------------------------------------------------------- #


def test_torn_final_line_is_skipped_and_reported(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    store.append(apply_entry())
    store.append(apply_entry(receipt="op_0002"))
    path = store.path_for(PROJECT)

    with path.open("ab") as handle:
        handle.write(b'{"seq": 3, "project_id": "reaper:{trunc')

    result = store.read()
    assert [entry["seq"] for entry in result["entries"]] == [1, 2]
    assert result["torn_line"] is True
    assert result["next_seq"] == 2

    # The next append separates the fragment instead of concatenating onto it.
    stored = store.append(apply_entry(receipt="op_0003"))
    assert stored["seq"] == 3
    result = store.read()
    assert [entry["seq"] for entry in result["entries"]] == [1, 2, 3]
    assert result["torn_line"] is False
    assert b'{"seq": 3, "project_id": "reaper:{trunc' in path.read_bytes()


def test_only_torn_fragment_reads_as_empty(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    path = store.path_for(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"seq": 1, "why": "unterminated')

    result = store.read()
    assert result["entries"] == []
    assert result["torn_line"] is True
    assert result["next_seq"] == 0

    # The first complete append starts at 1: the fragment was never an entry.
    assert store.append(apply_entry())["seq"] == 1


def test_complete_last_line_without_newline_is_not_torn(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    store.append(apply_entry())
    path = store.path_for(PROJECT)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"),
                    encoding="utf-8")

    result = store.read()
    assert [entry["seq"] for entry in result["entries"]] == [1]
    assert result["torn_line"] is False


def test_head_seq_survives_a_line_longer_than_the_tail_window(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    long_why = "x" * 70000
    first = store.append(apply_entry(why=long_why))
    second = store.append(apply_entry(receipt="op_0002"))

    assert (first["seq"], second["seq"]) == (1, 2)
    entries = store.read()["entries"]
    assert [entry["seq"] for entry in entries] == [1, 2]
    assert entries[0]["why"] == long_why


def test_head_seq_finds_long_line_before_a_torn_tail(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    long_why = "y" * 70000
    store.append(apply_entry(why=long_why))
    path = store.path_for(PROJECT)
    with path.open("ab") as handle:
        handle.write(b'{"seq": 2, "project_id": "torn')

    # A naive tail scan sees only the torn fragment and would reuse seq 1.
    assert store.append(apply_entry(receipt="op_0002"))["seq"] == 2
    result = store.read()
    assert [entry["seq"] for entry in result["entries"]] == [1, 2]
    assert result["torn_line"] is False


# --------------------------------------------------------------------------- #
# undo linkage
# --------------------------------------------------------------------------- #


def test_undo_entry_links_the_reversed_receipt(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    applied = store.append(apply_entry(receipt="op_0001"))

    undo = apply_entry(
        receipt="op_0002",
        operations=[operation(**{"volume_db": -1.0})],
        why="the cut sounded dull after the gain move",
        undoes=applied["receipt"],
    )
    assert undo["kind"] == "undo"
    assert undo["undoes"] == "op_0001"

    stored = store.append(undo)
    assert stored["kind"] == "undo"
    assert stored["undoes"] == "op_0001"
    assert stored["receipt"] == "op_0002"

    linked = store.read(receipt="op_0002")["entries"]
    assert len(linked) == 1
    assert linked[0]["undoes"] == applied["receipt"]

    assert [entry["seq"] for entry in store.read()["entries"]] == [1, 2]


def test_undo_cannot_name_itself(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        apply_entry(receipt="op_0009", undoes="op_0009")


def test_kind_and_undoes_must_agree(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    undo_without_target = apply_entry()
    undo_without_target["kind"] = "undo"
    with pytest.raises(ValidationError):
        store.append(undo_without_target)

    apply_with_target = apply_entry()
    apply_with_target["kind"] = "apply"
    apply_with_target["undoes"] = "op_0001"
    with pytest.raises(ValidationError):
        store.append(apply_with_target)


# --------------------------------------------------------------------------- #
# validation of the payload
# --------------------------------------------------------------------------- #


def test_receipt_may_be_a_receipt_object_or_mapping() -> None:
    class ReceiptLike:
        receipt_id = "op_object"

    assert apply_entry(receipt=ReceiptLike())["receipt"] == "op_object"
    assert apply_entry(receipt={"receipt_id": "op_mapping"})["receipt"] == "op_mapping"
    with pytest.raises(ValidationError):
        apply_entry(receipt=None)
    with pytest.raises(ValidationError):
        apply_entry(receipt="")


def test_receipt_evidence_is_attached_automatically() -> None:
    class ReceiptLike:
        receipt_id = "op_attached"
        readback = {"matched": True, "checked_operations": 1}
        verify = {"mode": "measurement", "matched": True}
        revision_before = {"token": 4, "revision": "rev-before"}
        revision_after = {"token": 5, "revision": "rev-after"}

    entry = apply_entry(receipt=ReceiptLike())
    assert entry["readback"] == {"matched": True, "checked_operations": 1}
    assert entry["verify"] == {"mode": "measurement", "matched": True}
    assert entry["revision_before"] == {"token": 4, "revision": "rev-before"}
    assert entry["revision_after"] == {"token": 5, "revision": "rev-after"}

    # An explicit argument wins over the receipt payload.
    explicit = apply_entry(receipt=ReceiptLike(),
                           readback={"matched": False, "reason": "caller"})
    assert explicit["readback"] == {"matched": False, "reason": "caller"}


@pytest.mark.parametrize(
    "basis, message",
    [
        ([{"kind": "vibe", "id": "x"}], "basis kind"),
        ([{"kind": "fact"}], "basis\\[0\\]\\.id"),
        ([{"kind": "fact", "id": "x", "note": "extra"}], "only kind and id"),
        ("fact:x", "basis"),
        ([{"kind": "fact", "id": ""}], "basis\\[0\\]\\.id"),
    ],
)
def test_basis_validation(basis: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        apply_entry(basis=basis)


def test_expected_must_be_a_json_object() -> None:
    with pytest.raises(ValidationError, match="JSON object"):
        apply_entry(expected=[1, 2, 3])
    with pytest.raises(ValidationError, match="JSON object"):
        apply_entry(expected="volume_db=-2")
    with pytest.raises(ValidationError, match="JSON object"):
        apply_entry(readback=["matched"])


def test_unencodable_text_is_refused() -> None:
    with pytest.raises(ValidationError, match="JSON-encodable"):
        apply_entry(why="broken \ud800 surrogate")
    with pytest.raises(ValidationError, match="NaN or infinity"):
        apply_entry(expected={"note": float("nan")})


@pytest.mark.parametrize(
    "operations, message",
    [
        ([], "at least one operation"),
        ([{"kind": "track.set"}], "target_guid"),
        ([{"target_guid": TRACK_A}], "kind"),
        ([{"kind": "track.set", "target_guid": TRACK_A, "values": "loud"}],
         "values"),
        ("track.set", "operations must be a list"),
    ],
)
def test_operation_validation(operations: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        apply_entry(operations=operations)


def test_basis_kinds_are_the_frozen_vocabulary() -> None:
    assert BASIS_KINDS == ("fact", "measurement", "receipt", "state")


# --------------------------------------------------------------------------- #
# secrets and raw audio
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"operations": [{"kind": "track.set", "target_guid": TRACK_A,
                          "values": {"password": "hunter2"}}]}, "secret"),
        ({"expected": {"api-key": "sk-live-123"}}, "secret"),
        ({"readback": {"samples": [0.0, 0.1, 0.2]}}, "raw audio"),
        ({"verify": {"pcm": [1, 2, 3]}}, "raw audio"),
        ({"fx_edits": [{"waveform": [0.0, 1.0]}]}, "raw audio"),
        ({"operations": [{"kind": "track.set", "target_guid": TRACK_A,
                          "values": {"blob": b"\x00\x01"}}]}, "binary"),
    ],
)
def test_secrets_and_raw_audio_are_refused(tmp_path: Path,
                                           overrides: dict,
                                           message: str) -> None:
    store = JournalStore(tmp_path, PROJECT)
    with pytest.raises(ValidationError, match=message):
        apply_entry(**overrides)
    assert not store.path_for(PROJECT).exists()


def test_unknown_top_level_field_is_refused(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    entry = apply_entry()
    entry["password"] = "hunter2"
    with pytest.raises(ValidationError, match="outside the line schema"):
        store.append(entry)
    assert not store.path_for(PROJECT).exists()


def test_successful_entry_never_contains_secret_or_raw_keys(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    stored = store.append(apply_entry(
        expected={"volume_db": -2.0, "note": "no credentials here"},
        readback={"matched": True, "values": {"volume_db": -2.0}},
    ))

    def walk(value: object) -> list[str]:
        if isinstance(value, dict):
            keys = [str(key).lower().replace("-", "_") for key in value]
            for item in value.values():
                keys.extend(walk(item))
            return keys
        if isinstance(value, list):
            keys = []
            for item in value:
                keys.extend(walk(item))
            return keys
        return []

    keys = set(walk(stored))
    assert not keys & SECRET_KEYS
    assert not keys & RAW_AUDIO_KEYS
    text = store.path_for(PROJECT).read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert "api_key" not in text


def test_secret_key_vocabulary_is_normalised() -> None:
    assert "password" in SECRET_KEYS
    assert "api_key" in SECRET_KEYS
    assert "samples" in RAW_AUDIO_KEYS
    assert "token" not in SECRET_KEYS  # the state token is ordinary data


# --------------------------------------------------------------------------- #
# timestamps, isolation and read resolution
# --------------------------------------------------------------------------- #


def test_timestamps_are_utc_iso_and_caller_offsets_normalise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal_module, "utc_now",
                        lambda: "2026-01-02T03:04:05.678Z")
    store = JournalStore(tmp_path, PROJECT)
    entry = apply_entry()
    assert entry["at"] == "2026-01-02T03:04:05.678Z"

    entry["at"] = "2026-01-02T05:04:05+02:00"
    stored = store.append(entry)
    assert stored["at"] == "2026-01-02T03:04:05.000Z"

    assert store.read()["entries"][0]["at"] == "2026-01-02T03:04:05.000Z"


def test_make_entry_copies_caller_data(tmp_path: Path) -> None:
    values = {"volume_db": -2.0}
    operations = [{"kind": "track.set", "target_guid": TRACK_A,
                   "values": values}]
    entry = apply_entry(operations=operations)
    values["volume_db"] = 12.0
    assert entry["operations"][0]["values"] == {"volume_db": -2.0}

    store = JournalStore(tmp_path, PROJECT)
    stored = store.append(entry)
    stored["why"] = "mutated after append"
    assert store.read()["entries"][0]["why"] == entry["why"]


def test_unbound_read_resolution(tmp_path: Path) -> None:
    empty = JournalStore(tmp_path)
    assert empty.read() == {"entries": [], "next_seq": 0, "torn_line": False}

    first = JournalStore(tmp_path)
    first.append(apply_entry())
    assert [entry["seq"] for entry in first.read()["entries"]] == [1]

    JournalStore(tmp_path).append(apply_entry(project_id=OTHER_PROJECT))

    ambiguous = JournalStore(tmp_path)
    with pytest.raises(ValidationError, match="several journals"):
        ambiguous.read()
    assert ambiguous.read(project_id=PROJECT)["next_seq"] == 1
    assert ambiguous.read(project_id=OTHER_PROJECT)["next_seq"] == 1

    bound = JournalStore(tmp_path).for_project(OTHER_PROJECT)
    assert bound.read()["entries"][0]["project_id"] == OTHER_PROJECT


def test_bound_store_refuses_a_foreign_entry(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, PROJECT)
    with pytest.raises(ValidationError, match="different project"):
        store.append(apply_entry(project_id=OTHER_PROJECT))
    assert not store.path_for(OTHER_PROJECT).exists()
