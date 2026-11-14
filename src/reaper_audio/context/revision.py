"""Revision adaptation and freshness predicates for the context graph.

:func:`from_project_revision` adapts the bridge's coarse project revision: with
no digest supplied it produces a *coarse placeholder* that can never claim
content equality, so a saved report is refused instead of being reused against
an unknown revision.

A digest measured elsewhere is adapted with :func:`digest_evidence` (or
:func:`revision_from_digest`).  It is only trusted as ``"full"`` when nothing is
reported missing, every field in :data:`DIGEST_REQUIRED_FIELDS` is covered *and*
each covered name is backed by the payload it promises in
:data:`DIGEST_COVERAGE_PATHS`.  A producer that sets a coverage flag without
shipping the matching payload does not gain a ``"full"`` scope from it: the
unbacked claim moves into ``missing`` and the scope degrades to ``"coarse"``.
A coverage flag that is switched off without being named in ``missing`` is
treated the same way, so a silent gap cannot pass for a measured one.

Nothing in this module talks to REAPER, opens a bridge mailbox, or reads the
clock.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .errors import ContractValidationError, RevisionMismatchError
from .identity import (ProjectIdentity, identity_complete as _identity_complete_value,
                       identity_gaps as _identity_gaps_value,
                       same_identity_values)
from .schema import FINGERPRINT_SCOPES, RevisionRef

__all__ = [
    "DIGEST_COVERAGE_ALIASES",
    "DIGEST_COVERAGE_PATHS",
    "DIGEST_REQUIRED_FIELDS",
    "DIGEST_VERSION",
    "FINGERPRINT_PREFIX",
    "FINGERPRINT_VERSION",
    "PLACEHOLDER_PREFIX",
    "DigestEvidence",
    "digest_coverage_gaps",
    "digest_evidence",
    "fingerprint_fields",
    "from_project_revision",
    "is_placeholder",
    "is_stale",
    "identity_gaps",
    "identity_complete",
    "merge_dep_sets",
    "require_same_content",
    "revision_from_digest",
    "same_content",
    "same_identity",
]

#: Marks a fingerprint that stands for "the content was never measured".
#: The value is ``unavailable:<project_id>:<change_count>``; it identifies the
#: snapshot for diagnostics but carries no content evidence.
PLACEHOLDER_PREFIX = "unavailable:"

#: Prefix of a real digest produced by :func:`fingerprint_fields`.
FINGERPRINT_PREFIX = "sha256:"

#: Version of the canonical payload hashed by :func:`fingerprint_fields`.
#: Bump it only together with a change to that payload's shape.
FINGERPRINT_VERSION = 1

#: Version of a project digest that :func:`digest_evidence` understands.
#: A digest with any other version is refused, not reinterpreted.
DIGEST_VERSION = 1

#: Fields a digest must have measured *and* backed with a payload before the
#: revision may call itself ``"full"``.  The list covers what the bridge
#: actually reads: per-track content, the master track, automation (track,
#: master, item and FX envelopes with their automation items), render/playback
#: project settings and the serialized FX chain state.  A digest that skips any
#: of them is ``"coarse"`` no matter how many other fields it carries, because
#: a report built on it cannot rule out a change in the unmeasured ones.
DIGEST_REQUIRED_FIELDS = frozenset({
    "tracks", "items", "takes", "fx", "sends", "tempo",
    "master", "automation", "project", "fx_state",
})

#: Where each coverage flag's evidence lives inside ``digest["fields"]``.  The
#: path is documentation for humans and tooling; enforcement uses the predicate
#: table below, which understands the nested shapes (takes/fx/sends live inside
#: each track or item row, not at a top-level key).  ``envelopes`` is a legacy
#: alias of ``automation`` and is deliberately absent here: only the canonical
#: name is checked, and the alias inherits its verdict.
DIGEST_COVERAGE_PATHS: Mapping[str, str] = MappingProxyType({
    "tracks": "fields.tracks",
    "items": "fields.items",
    "takes": "fields.items[*].take_guid",
    "fx": "fields.tracks[*].fx",
    "sends": "fields.tracks[*].sends",
    "tempo": "fields.tempo",
    "master": "fields.master",
    "automation": "fields.automation",
    "project": "fields.project",
    "fx_state": "fields.fx_state",
})

#: Coverage names that are spelling variants of one measured subgroup.  A gap
#: named once satisfies every name in its group, in either direction.
DIGEST_COVERAGE_ALIASES: Mapping[str, str] = MappingProxyType({
    "envelopes": "automation",
})

#: Payload keys the bridge must supply before ``project`` counts as covered.
#: They are the lower-cased ``GetSetProjectInfo`` names: render bounds, channels,
#: sample rate, tail, dither/normalize/brickwall/fades and the project sample
#: rate plus timebase.  Output file names and patterns are deliberately not
#: part of the digest: they select a destination, not project content.
_PROJECT_SETTING_KEYS = frozenset({
    "render_settings", "render_channels", "render_srate", "render_boundsflag",
    "render_startpos", "render_endpos", "render_tailflag", "render_tailms",
    "render_addtoproj", "render_dither", "render_normalize",
    "render_normalize_target", "render_brickwall", "render_fadein",
    "render_fadeout", "render_fadeinshape", "render_fadeoutshape",
    "render_fadelpf", "render_padstart", "render_padend", "render_trimstart",
    "render_trimend", "render_delay", "project_srate", "project_srate_use",
    "project_timebase", "project_timebase_flags",
})

_MASTER_PAYLOAD_KEYS = ("guid", "is_master", "volume", "pan", "mute", "solo",
                        "fx", "sends", "hardware_outputs")
_AUTOMATION_PAYLOAD_KEYS = ("tracks", "master", "items", "fx")


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field} must be a non-empty string", field=field)
    return value


def _require_non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{field} must be an integer", field=field)
    if value < 0:
        raise ContractValidationError(f"{field} must be >= 0", field=field)
    return value


def is_placeholder(fingerprint: str) -> bool:
    """True when ``fingerprint`` carries no content evidence."""
    return isinstance(fingerprint, str) and fingerprint.startswith(PLACEHOLDER_PREFIX)


def from_project_revision(
    project_id: str,
    path: str | None,
    change_count: int,
    fingerprint: str | None = None,
    instance_id: str | None = None,
    epoch: int = 0,
    fingerprint_scope: str = "full",
) -> RevisionRef:
    """Adapt the bridge's project revision to a :class:`RevisionRef`.

    ``change_count`` is REAPER's ``GetProjectStateChangeCount`` and becomes
    ``RevisionRef.counter``: it moves on every edit, but two different projects
    can share a value and an unchanged project can keep one across restarts, so
    it is diagnostic only and never proves content equality.

    With ``fingerprint=None`` (the only thing the bridge can support today) the
    result is a coarse placeholder ``unavailable:<project_id>:<change_count>``
    with ``fingerprint_scope="coarse"``.  Passing a ``fingerprint_scope`` of
    ``"full"`` together with ``fingerprint=None`` is therefore overridden to
    ``"coarse"``: an unmeasured revision must not look measured.

    ``path`` is accepted for signature compatibility with
    ``daw.session.ProjectRevision`` and validated, then dropped.  ``RevisionRef``
    has no path field, and the Lua identity already embeds the path inside
    ``project_id`` (``project_identity``, line 73).

    Raises ``ContractValidationError`` for a negative ``change_count`` or a
    malformed ``project_id``/``path``.  Never contacts REAPER.
    """
    _require_text(project_id, "project_id")
    if path is not None and not isinstance(path, str):
        raise ContractValidationError("path must be a string or None", field="path")
    counter = _require_non_negative_int(change_count, "change_count")

    if fingerprint is None:
        return RevisionRef(
            project_id=project_id,
            instance_id=instance_id,
            epoch=epoch,
            counter=counter,
            fingerprint=f"{PLACEHOLDER_PREFIX}{project_id}:{counter}",
            fingerprint_scope="coarse",
        )
    _require_text(fingerprint, "fingerprint")
    return RevisionRef(
        project_id=project_id,
        instance_id=instance_id,
        epoch=epoch,
        counter=counter,
        fingerprint=fingerprint,
        fingerprint_scope=fingerprint_scope,
    )


def _identity_of(revision: RevisionRef) -> ProjectIdentity:
    """Adapt a revision to the strict identity value used by reuse gates."""
    return ProjectIdentity(
        project_id=revision.project_id,
        instance_id=revision.instance_id,
        epoch=revision.epoch if revision.epoch >= 1 else None,
    )


def identity_gaps(revision: RevisionRef) -> tuple[str, ...]:
    """Identity components a revision cannot prove (empty when complete)."""
    return _identity_gaps_value(_identity_of(revision))


def identity_complete(revision: RevisionRef) -> bool:
    return _identity_complete_value(_identity_of(revision))


def same_identity(left: RevisionRef, right: RevisionRef) -> bool:
    """True only when both refs prove the same project in the same instance/epoch.

    A missing ``instance_id`` or a zero ``epoch`` is unknown, not equal.  The
    earlier Wave-A behaviour tolerated a missing instance for legacy records;
    that allowed a restarted REAPER on the same path to pass for continuous
    state, so it is deliberately gone.  Records written before the fields
    existed are ``incomplete`` and must be refreshed before reuse.
    """
    return same_identity_values(_identity_of(left), _identity_of(right))


def same_content(left: RevisionRef, right: RevisionRef) -> bool:
    """True only when both refs carry an equal, measured full-scope digest.

    Coarse refs and placeholders always answer ``False``: "same change_count"
    and "unavailable:..." are not evidence that the project content matched.
    """
    if left.project_id != right.project_id:
        return False
    if not same_identity(left, right):
        return False
    if left.fingerprint_scope != "full" or right.fingerprint_scope != "full":
        return False
    if is_placeholder(left.fingerprint) or is_placeholder(right.fingerprint):
        return False
    return left.fingerprint == right.fingerprint


def require_same_content(report_revision: RevisionRef, current_revision: RevisionRef) -> None:
    """Raise ``RevisionMismatchError`` unless :func:`same_content` holds."""
    if same_content(report_revision, current_revision):
        return
    raise RevisionMismatchError(
        "the report was produced against a different project revision "
        f"(report={report_revision.fingerprint!r}/{report_revision.fingerprint_scope}, "
        f"current={current_revision.fingerprint!r}/{current_revision.fingerprint_scope})",
        field="fingerprint",
        next_action="re-capture",
    )


def is_stale(
    report_revision: RevisionRef,
    current_revision: RevisionRef,
    dep_set: Iterable[str] | None = None,
    *,
    invalidated: Iterable[str] | None = None,
) -> bool:
    """True when a saved report may no longer describe the live project.

    A report is stale when the identity (project/instance/epoch) differs, when
    its measured content fingerprint differs, or when a dependency it was built
    from is known to have changed.

    Dependency semantics are deliberately fail-closed:

    * ``dep_set is None`` — legacy content comparison only; the caller stated
      no dependency claim, so none is invented.
    * ``dep_set`` empty — nothing to check against, content verdict stands.
    * ``dep_set`` non-empty and ``invalidated is None`` — freshness cannot be
      proven, so the report is stale.  Silence is not evidence that a
      dependency survived.
    * ``invalidated`` supplied — stale only when the two sets intersect;
      ``"*"``/``"all"`` stands for an unknown mutation scope and always
      invalidates.
    """
    if not same_identity(report_revision, current_revision):
        return True
    if not same_content(report_revision, current_revision):
        return True
    if dep_set is None and invalidated is None:
        return False
    dependencies = merge_dep_sets(dep_set)
    if invalidated is None:
        return bool(dependencies)
    invalid = {str(item) for item in invalidated}
    if invalid & {"*", "all"}:
        return True
    return bool(dependencies & invalid)


def _reject_non_finite(value: Any, field: str) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError(f"{field} is not finite", field="fields")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_non_finite(item, f"{field}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_finite(item, f"{field}[{index}]")


def fingerprint_fields(project_id: str, fields: Mapping[str, object]) -> str:
    """Hash ``fields`` into a stable ``sha256:`` digest for one project.

    The payload is ``{"v": 1, "project_id": ..., "fields": {...}}`` encoded as
    compact JSON with sorted keys and ASCII escaping, so equal content always
    produces the same digest regardless of insertion order, and the digest is
    reproducible outside Python.  ``project_id`` is part of the payload, so the
    same fields under two projects never collide.

    Non-finite floats are rejected instead of being encoded: JSON has no NaN or
    Infinity, and ``json.dumps`` would otherwise emit ``NaN`` and break readers.
    """
    _require_text(project_id, "project_id")
    if not isinstance(fields, Mapping):
        raise ContractValidationError("fields must be a mapping", field="fields")
    for key in fields:
        if not isinstance(key, str) or not key:
            raise ContractValidationError("field names must be non-empty strings", field="fields")
    _reject_non_finite(fields, "fields")
    payload = {"v": FINGERPRINT_VERSION, "project_id": project_id, "fields": dict(fields)}
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(
            f"fields are not canonically encodable: {exc}", field="fields"
        ) from exc
    return FINGERPRINT_PREFIX + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def merge_dep_sets(*sets: Iterable[str] | None) -> frozenset[str]:
    """Union several dependency sets into one frozenset.

    No semantics beyond set union: nothing is validated against the journal and
    no ordering is preserved.  A bare string is rejected because iterating it
    would silently split an id into characters; ``None`` is treated as an empty
    set so callers can forward an optional value.
    """
    merged: set[str] = set()
    for values in sets:
        if values is None:
            continue
        if isinstance(values, str):
            raise ContractValidationError(
                "a dependency set must be an iterable of ids, not a string", field="dep_set"
            )
        try:
            items = list(values)
        except TypeError as exc:
            raise ContractValidationError(
                "a dependency set must be iterable", field="dep_set"
            ) from exc
        for item in items:
            if not isinstance(item, str) or not item:
                raise ContractValidationError(
                    "dependency ids must be non-empty strings", field="dep_set"
                )
            merged.add(item)
    return frozenset(merged)


def _require_bool_mapping(value: Any, field: str) -> dict[str, bool]:
    """Copy a ``{name: bool}`` coverage map, rejecting implicit truthiness.

    ``0``/``1`` and ``"true"`` are refused: JSON stores real booleans, and a
    producer that sends ``1`` is ambiguous enough that guessing would let an
    unmeasured field masquerade as measured.
    """
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{field} must be a mapping", field=field)
    result: dict[str, bool] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ContractValidationError(f"{field} keys must be non-empty strings", field=field)
        if not isinstance(item, bool):
            raise ContractValidationError(
                f"{field}[{key!r}] must be a boolean, not {type(item).__name__}",
                field=field,
            )
        result[key] = item
    return result


def _require_str_sequence(value: Any, field: str) -> list[str]:
    """Copy a JSON array of names, rejecting a bare string (it iterates chars)."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ContractValidationError(f"{field} must be a list of strings", field=field)
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ContractValidationError(f"{field} must contain non-empty strings", field=field)
        result.append(item)
    return result


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    return False


def _rows_are_mappings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(row, Mapping) for row in value)


def _payload_tracks(fields: Mapping[str, object]) -> bool:
    rows = fields.get("tracks")
    return _rows_are_mappings(rows) and all(
        isinstance(row.get("guid"), str) for row in rows)  # type: ignore[union-attr]


def _payload_items(fields: Mapping[str, object]) -> bool:
    rows = fields.get("items")
    return _rows_are_mappings(rows) and all(
        isinstance(row.get("guid"), str) for row in rows)  # type: ignore[union-attr]


def _payload_takes(fields: Mapping[str, object]) -> bool:
    rows = fields.get("items")
    return _rows_are_mappings(rows) and all(
        "take_guid" in row for row in rows)  # type: ignore[union-attr]


def _payload_fx(fields: Mapping[str, object]) -> bool:
    rows = fields.get("tracks")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        return False
    for row in rows:
        fx = row.get("fx")
        if not isinstance(fx, list) or not all(isinstance(entry, Mapping) for entry in fx):
            return False
        if not all(isinstance(entry.get("params"), list) for entry in fx):
            return False
    return True


def _payload_sends(fields: Mapping[str, object]) -> bool:
    rows = fields.get("tracks")
    return _rows_are_mappings(rows) and all(
        _rows_are_mappings(row.get("sends")) for row in rows)  # type: ignore[union-attr]


def _payload_tempo(fields: Mapping[str, object]) -> bool:
    return isinstance(fields.get("tempo"), list)


def _payload_master(fields: Mapping[str, object]) -> bool:
    value = fields.get("master")
    if not isinstance(value, Mapping) or value.get("complete") is False:
        return False
    if not all(key in value for key in _MASTER_PAYLOAD_KEYS):
        return False
    for key in ("fx", "sends", "hardware_outputs"):
        if not _rows_are_mappings(value[key]):
            return False
    return all(isinstance(row.get("params"), list) for row in value["fx"])


def _payload_automation(fields: Mapping[str, object]) -> bool:
    value = fields.get("automation")
    if not isinstance(value, Mapping) or value.get("complete") is False:
        return False
    for key in _AUTOMATION_PAYLOAD_KEYS:
        if not _rows_are_mappings(value.get(key)):
            return False
    return all(isinstance(row.get("envelopes"), list) for row in value["tracks"])


def _payload_project(fields: Mapping[str, object]) -> bool:
    value = fields.get("project")
    if not isinstance(value, Mapping) or value.get("complete") is False:
        return False
    settings = value.get("settings")
    if not isinstance(settings, Mapping):
        return False
    if not _PROJECT_SETTING_KEYS.issubset(settings.keys()):
        return False
    return all(_is_finite_number(settings[key]) for key in _PROJECT_SETTING_KEYS)


def _payload_fx_state(fields: Mapping[str, object]) -> bool:
    value = fields.get("fx_state")
    if not isinstance(value, Mapping) or value.get("complete") is False:
        return False
    algorithm = value.get("algorithm")
    if not isinstance(algorithm, str) or not algorithm:
        return False
    rows = value.get("tracks")
    if not _rows_are_mappings(rows) or not rows:
        return False
    return all(isinstance(row.get("chunk"), str) and row.get("complete") is not False
               for row in rows)  # type: ignore[union-attr]


def _accept_unknown(fields: Mapping[str, object]) -> bool:
    """Verdict for a coverage name this reader has no payload rule for.

    Unknown names are not judged: a later schema may add a subgroup this reader
    cannot verify, and guessing either way would be worse than saying nothing.
    """
    return True


_COVERAGE_PAYLOAD_CHECKERS: Mapping[str, Callable[[Mapping[str, object]], bool]] = (
    MappingProxyType({
        "tracks": _payload_tracks,
        "items": _payload_items,
        "takes": _payload_takes,
        "fx": _payload_fx,
        "sends": _payload_sends,
        "tempo": _payload_tempo,
        "master": _payload_master,
        "automation": _payload_automation,
        "project": _payload_project,
        "fx_state": _payload_fx_state,
    }))


def _coverage_checker(name: str) -> Callable[[Mapping[str, object]], bool]:
    """Payload predicate for a coverage name, following legacy aliases."""
    canonical = DIGEST_COVERAGE_ALIASES.get(name, name)
    return _COVERAGE_PAYLOAD_CHECKERS.get(canonical, _accept_unknown)


def _coverage_group(name: str) -> frozenset[str]:
    """Names that stand for the same measured subgroup as ``name``."""
    canonical = DIGEST_COVERAGE_ALIASES.get(name, name)
    members = {canonical}
    members.update(alias for alias, target in DIGEST_COVERAGE_ALIASES.items()
                   if target == canonical)
    return frozenset(members)


def digest_coverage_gaps(
    fields: Mapping[str, object],
    coverage: Mapping[str, bool],
) -> tuple[str, ...]:
    """Coverage names that are switched on but carry no evidence.

    A flag alone is a claim, not a measurement.  Every name in
    :data:`DIGEST_COVERAGE_PATHS` has a predicate that checks the payload it
    promises; a claim without that payload is returned here so the caller can
    report it as missing instead of treating the project as measured.  Unknown
    names are not judged: a later schema may add a subgroup this reader cannot
    verify, and guessing either way would be worse than saying nothing.
    """
    if not isinstance(fields, Mapping):
        raise ContractValidationError("fields must be a mapping", field="fields")
    if not isinstance(coverage, Mapping):
        raise ContractValidationError("coverage must be a mapping", field="coverage")
    gaps: list[str] = []
    for name, measured in coverage.items():
        if not measured:
            continue
        if not _coverage_checker(name)(fields):
            gaps.append(DIGEST_COVERAGE_ALIASES.get(name, name))
    return tuple(sorted(set(gaps)))


@dataclass(frozen=True, slots=True)
class DigestEvidence:
    """What one project digest measured, what it missed, and how far to trust it.

    ``scope`` is ``"full"`` only when the digest covers every field in
    :data:`DIGEST_REQUIRED_FIELDS` with a payload this reader can verify, reports
    nothing missing, and carries at least one field; otherwise it is
    ``"coarse"``.  ``covered`` names the groups whose evidence was actually
    present (an unbacked coverage flag is *not* covered).  ``missing`` is the
    union of what the producer admitted and what it silently failed to back.
    Both are sorted tuples so two digests with the same evidence compare equal,
    and ``fingerprint`` is the digest of the evidence itself, never of the raw
    project.
    """

    fingerprint: str
    scope: str
    covered: tuple[str, ...]
    missing: tuple[str, ...]
    digest_version: int

    def __post_init__(self) -> None:
        _require_text(self.fingerprint, "fingerprint")
        if self.scope not in FINGERPRINT_SCOPES:
            raise ContractValidationError("scope must be 'full' or 'coarse'", field="scope")
        for name in ("covered", "missing"):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise ContractValidationError(f"{name} must be a tuple", field=name)
            for value in values:
                if not isinstance(value, str) or not value:
                    raise ContractValidationError(
                        f"{name} must contain non-empty strings", field=name
                    )
        if isinstance(self.digest_version, bool) or not isinstance(self.digest_version, int):
            raise ContractValidationError(
                "digest_version must be an integer", field="digest_version"
            )
        if self.digest_version != DIGEST_VERSION:
            raise ContractValidationError(
                f"unsupported digest_version {self.digest_version}", field="digest_version"
            )


def digest_evidence(project_id: str, digest: Mapping[str, object]) -> DigestEvidence:
    """Turn one bridge digest into evidence about what was actually measured.

    The digest schema (Lane 1, ``scripts/reaper_bridge.lua``) is::

        {"project_id": str, "path": str, "change_count": int,
         "digest_version": 1, "coverage": {name: bool},
         "missing": [name, ...],
         "fields": {tracks, items, tempo, master, automation, project, fx_state}}

    ``fields`` carries one payload per coverage name it can back: ``tracks``
    (level, pan, mute, solo, FX identity/order/state/parameters, sends and
    hardware outputs per row), ``items`` (placement, take, fades, stretch
    markers), ``tempo``, ``master`` (the same shape as one track row plus
    ``is_master``), ``automation`` (track/master/item/FX envelopes with points
    and automation items), ``project`` (render and playback settings) and
    ``fx_state`` (the serialized ``<FXCHAIN>`` text per track).

    Every key is required: an absent ``missing`` must not default to ``[]``,
    because that would silently upgrade an incomplete digest to ``"full"``.
    ``coverage`` must map names to real booleans and ``missing`` must be an
    array of names; neither is coerced.

    ``fingerprint`` hashes exactly ``{"digest_version": 1, "fields": ...,
    "coverage": ...}`` under ``project_id``, so it changes when any measured
    value *or* the set of measured fields changes, and two projects never
    collide.

    ``scope`` is ``"full"`` iff all four hold:

    1. ``missing`` is empty,
    2. every field in :data:`DIGEST_REQUIRED_FIELDS` is covered,
    3. each covered name is backed by the payload it promises in
       :data:`DIGEST_COVERAGE_PATHS`, and
    4. ``fields`` is non-empty.

    Anything else is ``"coarse"``.  That includes a digest that covers every
    required field but admits a missing one, a digest whose ``coverage`` claims
    everything while ``fields`` carries nothing to hash, a digest that switches
    a group off without naming it in ``missing``, and a digest that sets a flag
    for a payload it never shipped -- the unbacked claim is reported in
    ``missing`` instead of being honoured.

    Raises ``ContractValidationError`` for a malformed digest or an
    unsupported ``digest_version``.  ``change_count`` is validated here (it is
    part of the schema) but is not folded into the fingerprint: it moves on
    every edit, including edits that do not change any measured field.
    """
    _require_text(project_id, "project_id")
    if not isinstance(digest, Mapping):
        raise ContractValidationError("digest must be a mapping", field="digest")
    for key in ("digest_version", "change_count", "coverage", "missing", "fields"):
        if key not in digest:
            raise ContractValidationError(f"digest.{key} is required", field=key)

    version = digest["digest_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ContractValidationError("digest_version must be an integer", field="digest_version")
    if version != DIGEST_VERSION:
        raise ContractValidationError(
            f"unsupported digest_version {version}; this reader understands {DIGEST_VERSION} only",
            field="digest_version",
            next_action="upgrade-reader",
        )
    _require_non_negative_int(digest["change_count"], "change_count")
    coverage = _require_bool_mapping(digest["coverage"], "coverage")
    missing_fields = _require_str_sequence(digest["missing"], "missing")
    fields = digest["fields"]
    if not isinstance(fields, Mapping):
        raise ContractValidationError("fields must be a mapping", field="fields")
    for key in fields:
        if not isinstance(key, str) or not key:
            raise ContractValidationError("field names must be non-empty strings", field="fields")

    claimed = tuple(sorted(name for name, measured in coverage.items() if measured))
    backed = tuple(sorted(name for name in claimed if _coverage_checker(name)(fields)))
    unbacked = tuple(sorted({DIGEST_COVERAGE_ALIASES.get(name, name)
                             for name in claimed if name not in backed}))
    declared_missing = set(missing_fields)
    undeclared = tuple(sorted(
        name for name, measured in coverage.items()
        if not measured and not (_coverage_group(name) & declared_missing)
    ))
    # Anything the contract requires and this digest does not back is missing,
    # whether the producer switched it off explicitly or never mentioned it.
    unmeasured_required = DIGEST_REQUIRED_FIELDS - set(backed)
    missing = tuple(sorted(declared_missing | set(unbacked) | set(undeclared)
                           | unmeasured_required))
    is_full = (
        not missing
        and DIGEST_REQUIRED_FIELDS.issubset(backed)
        and bool(fields)
    )
    fingerprint = fingerprint_fields(
        project_id,
        {
            "digest_version": DIGEST_VERSION,
            "fields": dict(fields),
            "coverage": coverage,
        },
    )
    return DigestEvidence(
        fingerprint=fingerprint,
        scope="full" if is_full else "coarse",
        covered=backed,
        missing=missing,
        digest_version=DIGEST_VERSION,
    )


def revision_from_digest(
    project_id: str,
    change_count: int,
    digest: Mapping[str, object],
    instance_id: str | None = None,
    epoch: int = 0,
) -> RevisionRef:
    """Adapt a measured project digest to a :class:`RevisionRef`.

    ``fingerprint`` and ``fingerprint_scope`` come from :func:`digest_evidence`,
    so a partial digest yields a ``"coarse"`` ref that :func:`same_content`
    refuses to match.  ``counter`` is the caller's ``change_count`` (REAPER's
    ``GetProjectStateChangeCount``) and stays diagnostic only: it is not
    cross-checked against ``digest["change_count"]``, because the fingerprint,
    not the counter, is what proves content.

    Raises ``ContractValidationError`` for a malformed digest, a negative
    ``change_count``, or an unsupported digest version.  Never contacts REAPER.
    """
    counter = _require_non_negative_int(change_count, "change_count")
    evidence = digest_evidence(project_id, digest)
    return RevisionRef(
        project_id=project_id,
        instance_id=instance_id,
        epoch=epoch,
        counter=counter,
        fingerprint=evidence.fingerprint,
        fingerprint_scope=evidence.scope,
    )
