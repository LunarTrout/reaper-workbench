"""Contract tests for revision adaptation and freshness predicates.

No test here starts, connects to, or simulates REAPER: every value is built
locally, which is the point of the module under test.
"""

from __future__ import annotations

import importlib.util

import pytest

from reaper_audio.context.errors import ContractValidationError, RevisionMismatchError
from reaper_audio.context.revision import (
    DIGEST_COVERAGE_ALIASES,
    DIGEST_COVERAGE_PATHS,
    DIGEST_REQUIRED_FIELDS,
    FINGERPRINT_PREFIX,
    PLACEHOLDER_PREFIX,
    digest_coverage_gaps,
    fingerprint_fields,
    from_project_revision,
    is_placeholder,
    is_stale,
    merge_dep_sets,
    require_same_content,
    same_content,
    same_identity,
)
from reaper_audio.context.schema import RevisionRef

PROJECT = "reaper:{6A1B2C3D-0000-4000-8000-000000000001}:/songs/song.rpp"
OTHER_PROJECT = "reaper:{6A1B2C3D-0000-4000-8000-000000000002}:/songs/song.rpp"
OTHER_PATH_PROJECT = "reaper:{6A1B2C3D-0000-4000-8000-000000000001}:/songs/song-copy.rpp"


def _full(fingerprint: str = "sha256:" + "a" * 64, *, project_id: str = PROJECT,
          instance_id: str | None = "pri_01HZZZ", counter: int = 7) -> RevisionRef:
    return RevisionRef(project_id=project_id, instance_id=instance_id, epoch=1,
                       counter=counter, fingerprint=fingerprint,
                       fingerprint_scope="full")


def _legacy_revision_dict() -> dict[str, str | int]:
    """Exactly what ``ProjectRevision.to_dict()`` emits (daw/session.py:46-48)."""
    return {"project_id": PROJECT, "change_count": 41, "path": "/songs/song.rpp"}


# --------------------------------------------------------------------------
# adapter
# --------------------------------------------------------------------------


def test_missing_fingerprint_becomes_coarse_placeholder() -> None:
    revision = from_project_revision(PROJECT, "/songs/song.rpp", 41)

    assert revision.fingerprint == f"{PLACEHOLDER_PREFIX}{PROJECT}:41"
    assert revision.fingerprint_scope == "coarse"
    assert revision.counter == 41
    assert revision.project_id == PROJECT
    assert revision.instance_id is None
    assert revision.epoch == 0
    assert is_placeholder(revision.fingerprint)


def test_explicit_fingerprint_is_kept_with_full_scope() -> None:
    digest = fingerprint_fields(PROJECT, {"tracks": 12})
    revision = from_project_revision(PROJECT, "/songs/song.rpp", 41, digest,
                                     instance_id="pri_01HZZZ", epoch=3)

    assert revision.fingerprint == digest
    assert revision.fingerprint_scope == "full"
    assert revision.instance_id == "pri_01HZZZ"
    assert revision.epoch == 3
    assert not is_placeholder(revision.fingerprint)


def test_placeholder_overrides_an_explicit_full_scope() -> None:
    """An unmeasured revision must never be labelled ``full``."""
    revision = from_project_revision(PROJECT, None, 5, None, None, 0, "full")

    assert revision.fingerprint_scope == "coarse"


def test_legacy_project_revision_dict_adapts() -> None:
    legacy = _legacy_revision_dict()
    revision = from_project_revision(
        str(legacy["project_id"]), str(legacy["path"]), int(legacy["change_count"])
    )

    assert revision.counter == 41
    assert revision.project_id == legacy["project_id"]
    assert revision.fingerprint == f"{PLACEHOLDER_PREFIX}{PROJECT}:41"
    assert revision.to_dict()["fingerprint_scope"] == "coarse"


def test_real_project_revision_object_adapts() -> None:
    """Round-trip through the live dataclass when its audio deps are installed."""
    if importlib.util.find_spec("soundfile") is None or importlib.util.find_spec("scipy") is None:
        pytest.skip("reaper_audio.daw.session needs soundfile and scipy, absent here")
    from reaper_audio.daw.session import ProjectRevision

    legacy = ProjectRevision.from_dict(_legacy_revision_dict())
    revision = from_project_revision(str(legacy.project_id), legacy.path,
                                     int(legacy.change_count))

    assert revision.counter == legacy.change_count
    assert revision.fingerprint == f"{PLACEHOLDER_PREFIX}{PROJECT}:{legacy.change_count}"


@pytest.mark.parametrize("change_count", [-1, -100])
def test_negative_change_count_is_rejected(change_count: int) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        from_project_revision(PROJECT, "/songs/song.rpp", change_count)

    assert excinfo.value.field == "change_count"
    assert excinfo.value.code == "contract_validation"


def test_boolean_change_count_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        from_project_revision(PROJECT, "/songs/song.rpp", True)  # type: ignore[arg-type]

    assert excinfo.value.field == "change_count"


def test_empty_project_id_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        from_project_revision("", "/songs/song.rpp", 1)

    assert excinfo.value.field == "project_id"


# --------------------------------------------------------------------------
# identity vs content
# --------------------------------------------------------------------------


def test_same_identity_for_same_project() -> None:
    assert same_identity(_full(), _full())


def test_same_identity_fails_closed_when_a_component_is_unknown() -> None:
    """A missing instance cannot prove continuity, even against itself."""
    assert not same_identity(_full(instance_id=None), _full(instance_id="pri_01HZZZ"))
    assert not same_identity(_full(instance_id="pri_01HZZZ"), _full(instance_id=None))
    assert not same_identity(_full(instance_id=None), _full(instance_id=None))


def test_same_identity_rejects_a_different_epoch() -> None:
    left = RevisionRef(project_id=PROJECT, instance_id="pri_A", epoch=1, counter=7,
                       fingerprint="sha256:" + "a" * 64, fingerprint_scope="full")
    right = RevisionRef(project_id=PROJECT, instance_id="pri_A", epoch=2, counter=7,
                        fingerprint="sha256:" + "a" * 64, fingerprint_scope="full")
    assert not same_identity(left, right)
    assert same_identity(left, RevisionRef(
        project_id=PROJECT, instance_id="pri_A", epoch=1, counter=99,
        fingerprint="sha256:" + "b" * 64, fingerprint_scope="full"))


def test_same_identity_rejects_a_different_instance() -> None:
    assert not same_identity(_full(instance_id="pri_A"), _full(instance_id="pri_B"))


def test_same_identity_rejects_a_different_project() -> None:
    assert not same_identity(_full(), _full(project_id=OTHER_PROJECT))
    assert not same_identity(_full(), _full(project_id=OTHER_PATH_PROJECT))


def test_same_content_for_two_identical_full_fingerprints() -> None:
    assert same_content(_full(), _full(counter=99))


def test_placeholders_never_share_content() -> None:
    first = from_project_revision(PROJECT, "/songs/song.rpp", 41)
    second = from_project_revision(PROJECT, "/songs/song.rpp", 41)
    later = from_project_revision(PROJECT, "/songs/song.rpp", 42)

    assert first.fingerprint == second.fingerprint
    assert not same_content(first, second)
    assert not same_content(first, later)


def test_same_content_is_false_for_a_coarse_scope_even_with_equal_digests() -> None:
    coarse = RevisionRef(project_id=PROJECT, instance_id=None, epoch=0, counter=3,
                         fingerprint="sha256:" + "b" * 64, fingerprint_scope="coarse")

    assert not same_content(coarse, coarse)


def test_same_content_rejects_different_digests_and_projects() -> None:
    assert not same_content(_full(), _full("sha256:" + "c" * 64))
    assert not same_content(_full(), _full(project_id=OTHER_PROJECT))


def test_same_content_rejects_a_placeholder_against_a_real_digest() -> None:
    real = _full()
    placeholder = from_project_revision(PROJECT, "/songs/song.rpp", 7, None, "pri_01HZZZ", 1)

    assert not same_content(real, placeholder)
    assert not same_content(placeholder, real)


# --------------------------------------------------------------------------
# gates
# --------------------------------------------------------------------------


def test_require_same_content_accepts_an_identical_revision() -> None:
    require_same_content(_full(), _full())


def test_require_same_content_raises_with_a_machine_readable_reason() -> None:
    report = _full()
    current = _full("sha256:" + "d" * 64)

    with pytest.raises(RevisionMismatchError) as excinfo:
        require_same_content(report, current)

    error = excinfo.value
    assert error.code == "revision_mismatch"
    assert error.field == "fingerprint"
    assert error.next_action == "re-capture"
    assert error.to_dict()["code"] == "revision_mismatch"
    assert isinstance(error, ValueError)


def test_require_same_content_raises_for_a_placeholder_report() -> None:
    with pytest.raises(RevisionMismatchError):
        require_same_content(from_project_revision(PROJECT, "/songs/song.rpp", 41), _full())


def test_is_stale_tracks_same_content() -> None:
    current = _full()
    assert not is_stale(_full(), current)
    assert is_stale(_full("sha256:" + "e" * 64), current)
    assert is_stale(from_project_revision(PROJECT, "/songs/song.rpp", 41), current)


def test_is_stale_is_dependency_aware() -> None:
    """A dependency claim is either backed by invalidation evidence or refused."""
    current = _full()
    empty = merge_dep_sets()

    assert not is_stale(_full(), current, empty)
    # Non-empty dependencies with no invalidation evidence cannot be proven
    # fresh: silence is not evidence that a dependency survived.
    assert is_stale(_full(), current, frozenset({"rep_01", "cap_02"}))
    assert not is_stale(_full(), current, frozenset({"rep_01"}), invalidated=["cap_02"])
    assert is_stale(_full(), current, frozenset({"rep_01"}), invalidated=["rep_01"])
    assert is_stale(_full(), current, frozenset({"rep_01"}), invalidated=["*"])
    assert is_stale(_full("sha256:" + "f" * 64), current, frozenset({"rep_01"}))


# --------------------------------------------------------------------------
# fingerprint_fields
# --------------------------------------------------------------------------


def test_fingerprint_fields_is_stable_and_order_independent() -> None:
    first = fingerprint_fields(PROJECT, {"b": 2, "a": {"y": 1, "x": [1, 2]}})
    second = fingerprint_fields(PROJECT, {"a": {"x": [1, 2], "y": 1}, "b": 2})

    assert first == second
    assert first.startswith(FINGERPRINT_PREFIX)
    assert len(first) == len(FINGERPRINT_PREFIX) + 64
    assert fingerprint_fields(PROJECT, {"a": {"y": 1, "x": [1, 2]}, "b": 2}) == first


def test_fingerprint_fields_changes_with_values_and_project() -> None:
    base = fingerprint_fields(PROJECT, {"tracks": 12})
    assert fingerprint_fields(PROJECT, {"tracks": 13}) != base
    assert fingerprint_fields(PROJECT, {"tracks": 12, "dirty": False}) != base
    assert fingerprint_fields(OTHER_PROJECT, {"tracks": 12}) != base


def test_fingerprint_fields_is_not_a_placeholder() -> None:
    assert not is_placeholder(fingerprint_fields(PROJECT, {}))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_fingerprint_fields_rejects_non_finite_floats(bad: float) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        fingerprint_fields(PROJECT, {"gain": bad})

    assert excinfo.value.field == "fields"


def test_fingerprint_fields_rejects_a_nested_non_finite_float() -> None:
    with pytest.raises(ContractValidationError):
        fingerprint_fields(PROJECT, {"tracks": [{"gain": float("nan")}]})


def test_fingerprint_fields_rejects_non_string_keys() -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        fingerprint_fields(PROJECT, {1: "a"})  # type: ignore[dict-item]

    assert excinfo.value.field == "fields"


def test_fingerprint_fields_rejects_unencodable_values() -> None:
    with pytest.raises(ContractValidationError):
        fingerprint_fields(PROJECT, {"blob": object()})


def test_fingerprint_fields_rejects_an_empty_project_id() -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        fingerprint_fields("", {"a": 1})

    assert excinfo.value.field == "project_id"


# --------------------------------------------------------------------------
# digest coverage contract
# --------------------------------------------------------------------------


def _minimal_backed_fields() -> dict:
    return {
        "tracks": [{"guid": "{T}"}],
        "items": [{"guid": "{I}", "take_guid": "{K}"}],
        "tempo": [],
    }


def test_every_required_digest_field_has_a_documented_payload_path() -> None:
    """A required field without a payload rule could never be verified."""
    assert DIGEST_REQUIRED_FIELDS <= set(DIGEST_COVERAGE_PATHS)


def test_coverage_paths_point_into_the_fields_object() -> None:
    for name, path in DIGEST_COVERAGE_PATHS.items():
        assert isinstance(path, str) and path.startswith("fields."), name


def test_envelopes_is_only_an_alias_of_automation() -> None:
    assert DIGEST_COVERAGE_ALIASES == {"envelopes": "automation"}
    assert "envelopes" not in DIGEST_COVERAGE_PATHS


def test_coverage_gaps_is_empty_when_every_claim_is_backed() -> None:
    coverage = {"tracks": True, "items": True, "takes": True, "tempo": True,
                "envelopes": False}

    assert digest_coverage_gaps(_minimal_backed_fields(), coverage) == ()


def test_coverage_gaps_names_an_unbacked_claim() -> None:
    fields = _minimal_backed_fields()
    fields.pop("tempo")

    assert digest_coverage_gaps(fields, {"tracks": True, "tempo": True}) == ("tempo",)


def test_coverage_gaps_does_not_judge_an_unknown_name() -> None:
    assert digest_coverage_gaps(_minimal_backed_fields(),
                                {"tracks": True, "future_group": True}) == ()


def test_coverage_gaps_follows_the_envelopes_alias() -> None:
    fields = _minimal_backed_fields()

    assert digest_coverage_gaps(fields, {"envelopes": True}) == ("automation",)


def test_coverage_gaps_rejects_non_mappings() -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        digest_coverage_gaps([], {})  # type: ignore[arg-type]
    assert excinfo.value.field == "fields"

    with pytest.raises(ContractValidationError) as excinfo:
        digest_coverage_gaps({}, [])  # type: ignore[arg-type]
    assert excinfo.value.field == "coverage"


# --------------------------------------------------------------------------
# merge_dep_sets
# --------------------------------------------------------------------------


def test_merge_dep_sets_is_a_plain_union() -> None:
    merged = merge_dep_sets({"rep_01"}, ["cap_02", "rep_01"], None, frozenset())

    assert merged == frozenset({"rep_01", "cap_02"})
    assert isinstance(merged, frozenset)


def test_merge_dep_sets_of_nothing_is_empty() -> None:
    assert merge_dep_sets() == frozenset()
    assert merge_dep_sets(None, []) == frozenset()


def test_merge_dep_sets_rejects_a_bare_string_and_bad_members() -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        merge_dep_sets("rep_01")  # type: ignore[arg-type]
    assert excinfo.value.field == "dep_set"

    with pytest.raises(ContractValidationError):
        merge_dep_sets(["rep_01", ""])
