"""What was actually dialled on a plug-in, without dumping every parameter.

REAPER reports every parameter a plug-in exposes, including thousands of
generic slots (``#000``, ``Macro Parameter 3``) that say nothing about the
sound.  A project state should describe the edits, not that noise.

The policy implemented here is deliberately conservative:

* A plugin whose name marks it as an instrument (``VSTi:`` / ``VST3i:``) has
  no meaningful factory default: its parameter names and values describe the
  loaded preset.  Its reference is ``first_seen``.
* An effect is compared with a freshly inserted instance of the same plug-in.
  The comparison is only trusted when its parameter names are mostly
  descriptive; a plug-in exposing lots of ``#000``-style names may renumber
  them between instances, so name matching would be noise.
* Only parameters whose *normalized* value differs from the reference by more
  than :data:`DEFAULT_DELTA_EPSILON` are stored.

The named-ratio threshold is a caller-visible policy, not a plugin list:
:data:`DEFAULT_MIN_NAMED_RATIO` can be overridden per call.  Nothing in this
module knows a project, a track or a commercial plug-in by name.
"""

from __future__ import annotations

import inspect
import math
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypedDict

from ..errors import BridgeTimeout

#: Default share of descriptively named parameters required before a fresh
#: instance's parameter values are accepted as a meaningful reference.  A
#: plug-in where more than a quarter of the parameters are anonymous slots
#: (``#000``, blank, ``Macro Parameter N``) may renumber them between
#: instances, so name-based matching is not trustworthy.  Callers may inject
#: a different threshold into every public function that classifies rows.
DEFAULT_MIN_NAMED_RATIO = 0.75

#: Normalized values closer than this are equal for state purposes.  The
#: bridge rounds normalized values to float precision; a smaller epsilon would
#: report jitter as an edit.
DEFAULT_DELTA_EPSILON = 1e-6

#: Bounded cleanup attempt granted to a probe even after its main deadline.
#: Safety wins over the last fraction of a second: an instance that was
#: inserted must get one chance to be removed.
CLEANUP_GRACE_S = 5.0

_GENERIC_INDEX_NAME = re.compile(r"#\d+")
_INSTRUMENT_PREFIXES = ("vsti:", "vst3i:")

Kind = Literal["instrument", "effect"]
Reference = Literal["plugin_default", "first_seen"]
ProbeStatus = Literal["ok", "unavailable", "failed"]


class ParameterClassification(TypedDict):
    kind: Kind
    total: int
    generic_count: int
    named_ratio: float
    defaults_meaningful: bool
    reason: str


class SetParameter(TypedDict):
    name: str
    index: int
    value: float
    formatted: str
    default_value: float


class FxSummary(TypedDict):
    plugin: str
    kind: Kind
    total: int
    set_params_count: int
    available_params_count: int
    reference: Reference
    defaults_meaningful: bool
    set_params: list[SetParameter]


class ProbeResult(TypedDict):
    status: ProbeStatus
    parameters: list[dict[str, Any]]
    reason: str | None


class _ProbeTimeout(Exception):
    """Internal signal: the probe's own deadline expired."""


def _rows(value: Iterable[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    """Materialise rows, dropping non-mapping entries.

    Bridge responses are JSON; a malformed row is ignored rather than allowed
    to fail the whole summary.  Order is preserved.
    """
    if value is None:
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _name(row: Mapping[str, Any]) -> str:
    raw = row.get("name")
    return raw.strip() if isinstance(raw, str) else ""


def _is_instrument_name(plugin_name: str) -> bool:
    return plugin_name.strip().lower().startswith(_INSTRUMENT_PREFIXES)


def _is_generic_name(name: str) -> bool:
    """True for names that identify a slot rather than a control.

    Empty names, pure ``#<digits>`` slots and ``Macro Parameter N`` rows are
    the shapes REAPER/VST hosts emit for unnamed automatable values.
    """
    stripped = name.strip()
    if not stripped:
        return True
    if _GENERIC_INDEX_NAME.fullmatch(stripped):
        return True
    return "macro parameter" in stripped.lower()


def _as_index(value: Any) -> int | None:
    """Return a non-negative integer index, or ``None`` for malformed rows."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        index = value
    elif isinstance(value, float) and value.is_integer():
        index = int(value)
    else:
        return None
    return index if index >= 0 else None


def _as_normalized(value: Any) -> float | None:
    """Return a finite float, or ``None`` when the value is unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _validate_ratio(min_named_ratio: float) -> float:
    if (isinstance(min_named_ratio, bool) or
            not isinstance(min_named_ratio, (int, float)) or
            not math.isfinite(float(min_named_ratio)) or
            not 0.0 <= float(min_named_ratio) <= 1.0):
        raise ValueError("min_named_ratio must be a finite number from 0 to 1")
    return float(min_named_ratio)


def classify_parameters(
    rows: Iterable[Mapping[str, Any]] | None,
    plugin_name: str,
    *,
    min_named_ratio: float = DEFAULT_MIN_NAMED_RATIO,
) -> ParameterClassification:
    """Describe whether a fresh instance's parameter values are a useful default.

    An instrument is classified by its plugin name alone (``VSTi:`` /
    ``VST3i:``, case-insensitive); its rows are still counted so callers can
    report the size of the parameter surface.  For effects, ``named_ratio`` is
    the share of parameters with descriptive names, and
    ``defaults_meaningful`` is true only when that ratio reaches
    ``min_named_ratio``.  The threshold is inclusive and injectable; the
    default is :data:`DEFAULT_MIN_NAMED_RATIO`.
    """
    threshold = _validate_ratio(min_named_ratio)
    current = _rows(rows)
    total = len(current)
    generic_count = sum(1 for row in current if _is_generic_name(_name(row)))
    named_ratio = (total - generic_count) / total if total else 0.0
    instrument = _is_instrument_name(plugin_name)

    if instrument:
        return ParameterClassification(
            kind="instrument", total=total, generic_count=generic_count,
            named_ratio=named_ratio, defaults_meaningful=False,
            reason=("plugin name marks an instrument; parameter values describe "
                    "the loaded preset, not a meaningful default"),
        )
    if total == 0:
        return ParameterClassification(
            kind="effect", total=0, generic_count=0, named_ratio=0.0,
            defaults_meaningful=False,
            reason="plugin exposes no parameters; there is no default to compare",
        )
    if named_ratio >= threshold:
        return ParameterClassification(
            kind="effect", total=total, generic_count=generic_count,
            named_ratio=named_ratio, defaults_meaningful=True,
            reason=(f"descriptive parameter names dominate "
                    f"({named_ratio:.3f} named); a fresh instance is a usable default"),
        )
    return ParameterClassification(
        kind="effect", total=total, generic_count=generic_count,
        named_ratio=named_ratio, defaults_meaningful=False,
        reason=(f"generic parameter names dominate ({generic_count}/{total} generic, "
                f"named ratio {named_ratio:.3f} < {threshold:.3f}); names cannot be "
                f"matched reliably between instances"),
    )


def _unique_named_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Index rows by stripped name, dropping every name that occurs twice.

    A duplicated name is ambiguous: matching either copy would make the result
    depend on row order, so both are skipped.  Blank names are never matched.
    """
    unique: dict[str, Mapping[str, Any]] = {}
    duplicated: set[str] = set()
    for row in rows:
        name = _name(row)
        if not name:
            continue
        if name in unique:
            duplicated.add(name)
        else:
            unique[name] = row
    for name in duplicated:
        unique.pop(name, None)
    return unique


def diff_set_params(
    current_rows: Iterable[Mapping[str, Any]] | None,
    defaults_rows: Iterable[Mapping[str, Any]] | None,
    *,
    epsilon: float = DEFAULT_DELTA_EPSILON,
) -> list[SetParameter]:
    """Parameters whose normalized value differs from the default instance.

    Rows are matched by stripped parameter name.  A name missing from either
    side, appearing more than once on either side, or carrying a malformed
    index/normalized value is skipped deterministically.  Results are sorted by
    the current row's parameter index, then name.

    ``epsilon`` is the "still equal" band: only ``abs(current - default) >
    epsilon`` is reported, so floating-point noise is not an edit.
    """
    if isinstance(epsilon, bool) or not isinstance(epsilon, (int, float)):
        raise ValueError("epsilon must be a finite non-negative number")
    tolerance = float(epsilon)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("epsilon must be a finite non-negative number")

    current = _unique_named_rows(_rows(current_rows))
    defaults = _unique_named_rows(_rows(defaults_rows))
    changes: list[SetParameter] = []
    for name, row in current.items():
        reference = defaults.get(name)
        if reference is None:
            continue
        index = _as_index(row.get("index"))
        value = _as_normalized(row.get("normalized"))
        default_value = _as_normalized(reference.get("normalized"))
        if index is None or value is None or default_value is None:
            continue
        if abs(value - default_value) <= tolerance:
            continue
        formatted = row.get("formatted")
        changes.append(SetParameter(
            name=name, index=index, value=value,
            formatted=formatted if isinstance(formatted, str) else "",
            default_value=default_value,
        ))
    changes.sort(key=lambda item: (item["index"], item["name"]))
    return changes


def _matched_names(
    current: Iterable[Mapping[str, Any]],
    defaults: Iterable[Mapping[str, Any]],
) -> set[str]:
    """Names that can be compared: unique, non-blank and present on both sides."""
    return set(_unique_named_rows(current)) & set(_unique_named_rows(defaults))


def summarize_fx(
    plugin_name: str,
    current_rows: Iterable[Mapping[str, Any]] | None,
    defaults_rows: Iterable[Mapping[str, Any]] | None = None,
    *,
    min_named_ratio: float = DEFAULT_MIN_NAMED_RATIO,
) -> FxSummary:
    """Summarize one live plug-in instance relative to its reference.

    ``set_params`` contains only differences.  ``reference`` is
    ``plugin_default`` when the plug-in is an effect, its names pass the
    named-ratio policy, and a non-empty default listing was supplied.  In every
    other case the reference is ``first_seen`` and ``set_params`` is empty:
    there is nothing to compare yet, and the caller records later edits against
    the first observation.

    ``available_params_count`` is the number of unique parameter names that
    exist on both sides and could therefore be compared; it is 0 whenever the
    reference is ``first_seen``.  ``total`` is every parameter the live
    instance exposes, including generic slots.
    """
    current = _rows(current_rows)
    defaults = _rows(defaults_rows)
    classification = classify_parameters(
        current, plugin_name, min_named_ratio=min_named_ratio)
    if (classification["kind"] == "effect" and
            classification["defaults_meaningful"] and defaults):
        available = len(_matched_names(current, defaults))
        set_params = diff_set_params(current, defaults, epsilon=DEFAULT_DELTA_EPSILON)
        reference: Reference = "plugin_default"
    else:
        available = 0
        set_params = []
        reference = "first_seen"
    return FxSummary(
        plugin=plugin_name,
        kind=classification["kind"],
        total=classification["total"],
        set_params_count=len(set_params),
        available_params_count=available,
        reference=reference,
        defaults_meaningful=classification["defaults_meaningful"],
        set_params=set_params,
    )


def _bridge(session: Any) -> Any:
    """The object exposing ``call``: a session's bridge, or the session itself."""
    return getattr(session, "bridge", session)


def _bridge_call(bridge: Any, operation: str, arguments: dict[str, Any],
                 timeout_s: float) -> Any:
    """Call the bridge, passing a timeout only when its signature accepts one.

    Tests and alternative transports expose two- or three-argument ``call``
    methods; both are supported without swallowing a TypeError raised inside a
    successful call.
    """
    call = getattr(bridge, "call", None)
    if not callable(call):
        raise TypeError("session has no bridge.call")
    try:
        parameters = inspect.signature(call).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_timeout = ("timeout_s" in parameters or
                       any(item.kind is inspect.Parameter.VAR_KEYWORD
                           for item in parameters.values()))
    if accepts_timeout:
        return call(operation, arguments, timeout_s=timeout_s)
    return call(operation, arguments)


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _ProbeTimeout("probe deadline expired")
    return left


def _cleanup_timeout(deadline: float, fallback_s: float) -> float:
    """Time budget for cleanup: remaining time, or a bounded grace period."""
    left = deadline - time.monotonic()
    return left if left > 0 else max(0.1, fallback_s)


def _guid_from_result(result: Any) -> str:
    """Extract the inserted instance's GUID from an insert result."""
    if isinstance(result, str):
        return result
    if not isinstance(result, Mapping):
        return ""
    direct = result.get("fx_guid") or result.get("guid")
    if isinstance(direct, str) and direct:
        return direct
    rows = result.get("operation_results")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping):
                guid = row.get("fx_guid") or row.get("guid")
                if isinstance(guid, str) and guid:
                    return guid
    return ""


def _check_deleted(result: Any, fx_guid: str) -> None:
    """Refuse a cleanup result that names a different instance or says no-op."""
    if not isinstance(result, Mapping):
        return
    if result.get("deleted") is False or result.get("changed") is False:
        raise RuntimeError("bridge reported that the probe instance was not removed")
    candidates: list[Any] = [result.get("deleted_fx_guid")]
    rows = result.get("operation_results")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping):
                candidates.append(row.get("deleted_fx_guid"))
                if row.get("changed") is False:
                    raise RuntimeError(
                        "bridge reported that the probe instance was not removed")
    for candidate in candidates:
        if (isinstance(candidate, str) and candidate and
                candidate != fx_guid):
            raise RuntimeError("cleanup targeted a different instance")


def _insert_instance(session: Any, plugin_name: str, track_guid: str,
                     slot: int, deadline: float) -> str:
    """Insert one fresh instance and return its GUID."""
    planner = getattr(session, "plan", None)
    applier = getattr(session, "apply", None)
    if callable(planner) and callable(applier):
        from .session import EditOperation

        values: dict[str, Any] = {"name": plugin_name}
        if slot >= 0:
            values["position"] = slot
        _remaining(deadline)
        plan = planner(
            [EditOperation(kind="fx.insert", target_guid=track_guid, values=values)],
            reason="fx_state: probe plugin defaults",
        )
        _remaining(deadline)
        result = applier(plan, dry_run=False)
        return _guid_from_result(result)

    arguments: dict[str, Any] = {"track_guid": track_guid, "name": plugin_name}
    if slot >= 0:
        arguments["position"] = slot
    result = _bridge_call(_bridge(session), "fx.insert", arguments,
                          _remaining(deadline))
    return _guid_from_result(result)


def _read_instance_parameters(session: Any, track_guid: str, fx_guid: str,
                              deadline: float) -> list[dict[str, Any]]:
    """Read the freshly inserted instance's rows through ``fx.list_params``."""
    response = _bridge_call(
        _bridge(session), "fx.list_params",
        {"track_guid": track_guid, "fx_guid": fx_guid},
        _remaining(deadline),
    )
    if not isinstance(response, Mapping):
        raise TypeError("fx.list_params returned no object")
    rows = response.get("parameters")
    if not isinstance(rows, list):
        raise TypeError("fx.list_params returned no parameter list")
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _remove_instance(session: Any, track_guid: str, fx_guid: str,
                     deadline: float) -> None:
    """Remove exactly the instance the probe inserted."""
    timeout_s = _cleanup_timeout(deadline, CLEANUP_GRACE_S)
    planner = getattr(session, "plan", None)
    applier = getattr(session, "apply", None)
    if callable(planner) and callable(applier):
        from .session import EditOperation

        plan = planner(
            [EditOperation(kind="fx.delete", target_guid=track_guid,
                           values={"fx_guid": fx_guid})],
            reason="fx_state: remove probe instance",
        )
        result = applier(plan, dry_run=False)
        _check_deleted(result, fx_guid)
        return
    result = _bridge_call(_bridge(session), "fx.delete",
                          {"track_guid": track_guid, "fx_guid": fx_guid},
                          timeout_s)
    _check_deleted(result, fx_guid)


def _probe_result(status: ProbeStatus, parameters: list[dict[str, Any]],
                  reason: str | None) -> ProbeResult:
    return ProbeResult(status=status, parameters=parameters, reason=reason)


def probe_defaults(
    session: Any,
    plugin_name: str,
    *,
    track_guid: str,
    slot: int = -1,
    timeout_s: float = 30.0,
) -> ProbeResult:
    """Measure a fresh instance's parameters on an empty scratch track.

    The caller owns ``track_guid`` and must pass an empty scratch track: this
    function inserts one instance, reads its ``fx.list_params`` rows and then
    removes that exact GUID.  It never inserts an instrument; a ``VSTi:`` /
    ``VST3i:`` name returns ``unavailable`` with
    ``reason='instrument_no_defaults'`` before any bridge call is made.

    Removal failure is explicit: if the bridge cannot prove the inserted
    instance was removed, the result is ``failed`` with
    ``reason='cleanup_unsupported'`` and no parameters.  Other failure reasons
    are stable codes (``insert_failed``, ``parameter_read_failed``,
    ``timeout``, ``invalid_track_guid``, ``invalid_plugin_name``,
    ``invalid_slot``, ``invalid_timeout``); the function never raises.

    ``slot=-1`` appends the instance to the chain; a non-negative slot asks the
    edit layer to insert at that position.  The timeout bounds the insert and
    read calls (and each bridge call's own timeout); cleanup gets one bounded
    attempt even after the deadline so a timed-out read cannot silently leave
    a plug-in behind.
    """
    if not isinstance(plugin_name, str) or not plugin_name.strip():
        return _probe_result("failed", [], "invalid_plugin_name")
    if _is_instrument_name(plugin_name):
        return _probe_result("unavailable", [], "instrument_no_defaults")
    if not isinstance(track_guid, str) or not track_guid.strip():
        return _probe_result("failed", [], "invalid_track_guid")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < -1:
        return _probe_result("failed", [], "invalid_slot")
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or
            not math.isfinite(float(timeout_s)) or float(timeout_s) <= 0):
        return _probe_result("failed", [], "invalid_timeout")

    deadline = time.monotonic() + float(timeout_s)
    try:
        fx_guid = _insert_instance(session, plugin_name.strip(), track_guid,
                                   slot, deadline)
    except (BridgeTimeout, TimeoutError, _ProbeTimeout):
        return _probe_result("failed", [], "timeout")
    except Exception:  # noqa: BLE001 - probe must never raise to its caller
        return _probe_result("failed", [], "insert_failed")
    if not fx_guid:
        # The edit may have happened; without a GUID its removal cannot be
        # targeted.  Fail loudly instead of pretending or deleting by guess.
        return _probe_result("failed", [], "insert_failed")

    read_reason: str | None = None
    parameters: list[dict[str, Any]] = []
    try:
        parameters = _read_instance_parameters(session, track_guid, fx_guid,
                                               deadline)
    except (BridgeTimeout, TimeoutError, _ProbeTimeout):
        read_reason = "timeout"
    except Exception:  # noqa: BLE001 - reported as a failed probe
        read_reason = "parameter_read_failed"

    try:
        _remove_instance(session, track_guid, fx_guid, deadline)
    except Exception:  # noqa: BLE001 - unsupported or unverified cleanup
        return _probe_result("failed", [], "cleanup_unsupported")

    if read_reason is not None:
        return _probe_result("failed", [], read_reason)
    return _probe_result("ok", parameters, None)
