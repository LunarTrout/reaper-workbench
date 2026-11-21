"""State should carry the dialled values, not every anonymous parameter slot."""

from __future__ import annotations

import pytest

from reaper_audio.daw.fx_state import (CLEANUP_GRACE_S, DEFAULT_DELTA_EPSILON,
                                       DEFAULT_MIN_NAMED_RATIO,
                                       classify_parameters, diff_set_params,
                                       probe_defaults, summarize_fx)


def row(index: int, name: str, normalized: float, formatted: str = "") -> dict:
    return {"index": index, "name": name, "normalized": normalized,
            "formatted": formatted}


def rows(count: int, name_at, normalized: float = 0.5) -> list[dict]:
    return [row(index, name_at(index), normalized) for index in range(count)]


def proq_like(count: int = 603) -> list[dict]:
    """A well-named effect with a large but descriptive parameter surface."""
    return rows(count, lambda index: f"Band {index // 8 + 1} Control {index % 8}")


def kontakt_like(count: int = 4148, generic: int = 2048) -> list[dict]:
    """An instrument whose surface is mostly anonymous host slots."""
    return rows(count, lambda index: f"#{index:04d}" if index < generic
                else f"Host Channel {index} Volume")


# --------------------------------------------------------------------------
# classification


def test_classify_proq_like_effect_accepts_a_fresh_instance_as_default() -> None:
    listing = proq_like()
    result = classify_parameters(listing, "VST3: Pro-Q 4 (FabFilter)")

    assert result["kind"] == "effect"
    assert result["total"] == 603
    assert result["generic_count"] == 0
    assert result["named_ratio"] == pytest.approx(1.0)
    assert result["defaults_meaningful"] is True
    assert "usable default" in result["reason"]


def test_classify_kontakt_like_instrument_never_trusts_plugin_defaults() -> None:
    listing = kontakt_like()
    result = classify_parameters(listing, "VSTi: Kontakt 7 (Native Instruments)")

    assert result["kind"] == "instrument"
    assert result["total"] == 4148
    assert result["generic_count"] == 2048
    assert result["named_ratio"] == pytest.approx((4148 - 2048) / 4148)
    assert result["defaults_meaningful"] is False
    assert "instrument" in result["reason"]
    # the same rows used by an effect would be refused on the ratio alone
    assert result["named_ratio"] < DEFAULT_MIN_NAMED_RATIO


def test_classify_is_case_insensitive_about_instrument_prefixes() -> None:
    listing = [row(0, "Cutoff", 0.5)]
    assert classify_parameters(listing, "vst3i: Some Synth")["kind"] == "instrument"
    assert classify_parameters(listing, "vsti: Some Sampler")["kind"] == "instrument"


def test_classify_effect_with_generic_names_uses_an_injectable_threshold() -> None:
    listing = [
        row(0, "", 0.5), row(1, "#001", 0.5), row(2, "#002", 0.5),
        row(3, "Macro Parameter 1", 0.5), row(4, "Macro Parameter 2", 0.5),
        row(5, "Cutoff", 0.5), row(6, "Resonance", 0.5), row(7, "Drive", 0.5),
        row(8, "Mix", 0.5), row(9, "Output", 0.5),
    ]

    default = classify_parameters(listing, "VST: Generic Thing")
    assert default["kind"] == "effect"
    assert default["generic_count"] == 5
    assert default["named_ratio"] == pytest.approx(0.5)
    assert default["defaults_meaningful"] is False
    assert "generic" in default["reason"]

    relaxed = classify_parameters(listing, "VST: Generic Thing",
                                  min_named_ratio=0.5)
    assert relaxed["defaults_meaningful"] is True

    with pytest.raises(ValueError):
        classify_parameters(proq_like(8), "VST: Named Thing",
                            min_named_ratio=1.01)


def test_classify_empty_listing_has_no_defaults() -> None:
    result = classify_parameters([], "VST: Silent")
    assert result["total"] == 0
    assert result["named_ratio"] == 0.0
    assert result["defaults_meaningful"] is False
    assert "no parameters" in result["reason"]


# --------------------------------------------------------------------------
# diffing


def test_diff_reports_only_parameters_past_the_epsilon() -> None:
    defaults = [
        row(0, "Band 1 Freq", 0.40, "1.00 kHz"),
        row(1, "Band 1 Gain", 0.50, "0.0 dB"),
        row(2, "Band 1 Q", 0.30, "1.00"),
        row(3, "Output Gain", 0.50, "0.0 dB"),
        row(4, "Bypass", 0.0, "Off"),
        row(5, "Duplicate", 0.10),
        row(6, "Duplicate", 0.20),
        row(7, "", 0.0),
    ]
    current = [
        row(4, "Bypass", 1.0, "On"),
        row(3, "Output Gain", 0.45, "-1.0 dB"),
        row(2, "Band 1 Q", 0.30, "1.00"),
        row(1, "Band 1 Gain", 0.50 + DEFAULT_DELTA_EPSILON / 2, "0.0 dB"),
        row(0, "Band 1 Freq", 0.55, "2.00 kHz"),
        row(5, "Duplicate", 0.90),
        row(6, "Duplicate", 0.95),
        row(7, "", 0.99),
        row(8, "Only Here", 0.77),
    ]

    changes = diff_set_params(current, defaults)

    assert [item["name"] for item in changes] == ["Band 1 Freq", "Output Gain", "Bypass"]
    assert [item["index"] for item in changes] == [0, 3, 4]
    assert changes[0]["value"] == pytest.approx(0.55)
    assert changes[0]["default_value"] == pytest.approx(0.40)
    assert changes[0]["formatted"] == "2.00 kHz"


def test_diff_epsilon_is_injectable() -> None:
    near = DEFAULT_DELTA_EPSILON / 2
    current = [row(0, "Gain", 0.5 + near)]
    defaults = [row(0, "Gain", 0.5)]
    assert diff_set_params(current, defaults) == []
    assert len(diff_set_params(current, defaults, epsilon=0.0)) == 1


def test_diff_skips_malformed_and_unmatched_rows() -> None:
    current = [
        {"index": 0, "name": "A", "normalized": "0.9"},
        {"index": 1, "name": "B", "normalized": float("nan")},
        {"index": -2, "name": "C", "normalized": 0.9},
        {"index": 3, "name": "D", "normalized": 0.9},
    ]
    defaults = [
        row(0, "A", 0.1), row(1, "B", 0.1), row(2, "C", 0.1), row(3, "D", 0.9),
    ]
    assert diff_set_params(current, defaults) == []


# --------------------------------------------------------------------------
# summaries


def test_summarize_effect_stores_only_the_differences_from_plugin_default() -> None:
    current = proq_like()
    defaults = proq_like()
    changed: list[int] = []
    for index in range(0, 603, 28):
        current[index]["normalized"] = 0.7
        current[index]["formatted"] = "6.00 dB"
        defaults[index]["normalized"] = 0.4
        changed.append(index)

    summary = summarize_fx("VST3: Pro-Q 4 (FabFilter)", current, defaults)

    assert summary["plugin"] == "VST3: Pro-Q 4 (FabFilter)"
    assert summary["kind"] == "effect"
    assert summary["total"] == 603
    assert summary["reference"] == "plugin_default"
    assert summary["defaults_meaningful"] is True
    assert summary["available_params_count"] == 603
    assert summary["set_params_count"] == len(changed) == 22
    assert len(summary["set_params"]) == 22
    assert [item["index"] for item in summary["set_params"]] == changed
    assert all(item["value"] == pytest.approx(0.7) for item in summary["set_params"])
    assert all(item["default_value"] == pytest.approx(0.4)
               for item in summary["set_params"])


def test_summarize_without_defaults_falls_back_to_first_seen() -> None:
    summary = summarize_fx("VST3: Pro-Q 4 (FabFilter)", proq_like())

    assert summary["reference"] == "first_seen"
    assert summary["set_params"] == []
    assert summary["set_params_count"] == 0
    assert summary["available_params_count"] == 0
    assert summary["defaults_meaningful"] is True
    # an empty default listing is just as absent as None
    assert summarize_fx("VST3: Pro-Q 4 (FabFilter)", proq_like(), [])["reference"] == "first_seen"


def test_summarize_instrument_ignores_supplied_defaults() -> None:
    summary = summarize_fx("VSTi: Kontakt 7 (Native Instruments)",
                           kontakt_like(), kontakt_like())

    assert summary["kind"] == "instrument"
    assert summary["reference"] == "first_seen"
    assert summary["set_params"] == []
    assert summary["set_params_count"] == 0
    assert summary["available_params_count"] == 0
    assert summary["defaults_meaningful"] is False


def test_summarize_counts_only_names_available_on_both_sides() -> None:
    current = [row(0, "Cutoff", 0.8), row(1, "Resonance", 0.4),
               row(2, "Live Only", 0.9)]
    defaults = [row(0, "Cutoff", 0.2), row(1, "Resonance", 0.4),
                row(2, "Default Only", 0.1)]

    summary = summarize_fx("VST: Filter", current, defaults)
    assert summary["total"] == 3
    assert summary["available_params_count"] == 2
    assert summary["set_params_count"] == 1
    assert summary["set_params"][0]["name"] == "Cutoff"


# --------------------------------------------------------------------------
# probe


class FakeBridge:
    """Bridge double: one installed instance per probe, at a fixed GUID."""

    def __init__(self, *, delete_supported: bool = True,
                 read_error: Exception | None = None) -> None:
        self.delete_supported = delete_supported
        self.read_error = read_error
        self.calls: list[tuple[str, dict, float]] = []
        self.inserted_guids: list[str] = []
        self.parameters = [row(0, "Cutoff", 0.2, "200 Hz"),
                           row(1, "Resonance", 0.3, "0.3")]

    def call(self, operation: str, arguments: dict, timeout_s: float = 10.0):
        self.calls.append((operation, dict(arguments), timeout_s))
        if operation == "fx.insert":
            guid = "{PROBE_FX_1}"
            self.inserted_guids.append(guid)
            return {"changed": True, "fx_guid": guid}
        if operation == "fx.list_params":
            if self.inserted_guids:
                assert arguments["fx_guid"] == self.inserted_guids[-1]
            if self.read_error is not None:
                raise self.read_error
            return {"parameters": [dict(item) for item in self.parameters]}
        if operation == "fx.delete":
            if not self.delete_supported:
                raise NotImplementedError("fx.delete is not available")
            return {"changed": True,
                    "deleted_fx_guid": arguments["fx_guid"]}
        raise AssertionError(f"unexpected bridge operation {operation}")


class FakeSession:
    def __init__(self, bridge) -> None:
        self.bridge = bridge


class ExplodingBridge:
    def __init__(self) -> None:
        self.calls = 0

    def call(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("an instrument probe must not touch the bridge")


def test_probe_inserts_reads_and_removes_exactly_its_instance() -> None:
    bridge = FakeBridge()
    result = probe_defaults(FakeSession(bridge), "VST3: Pro-Q 4 (FabFilter)",
                            track_guid="{SCRATCH}", slot=2)

    assert result == {"status": "ok",
                      "parameters": bridge.parameters,
                      "reason": None}
    assert [call[0] for call in bridge.calls] == [
        "fx.insert", "fx.list_params", "fx.delete"]
    insert_args = bridge.calls[0][1]
    assert insert_args["track_guid"] == "{SCRATCH}"
    assert insert_args["name"] == "VST3: Pro-Q 4 (FabFilter)"
    assert insert_args["position"] == 2
    assert bridge.calls[2][1]["fx_guid"] == "{PROBE_FX_1}"
    assert bridge.calls[2][1]["track_guid"] == "{SCRATCH}"
    assert all(call[2] > 0 for call in bridge.calls)


def test_probe_refuses_an_instrument_before_any_insert() -> None:
    bridge = ExplodingBridge()
    result = probe_defaults(FakeSession(bridge), "VSTi: Kontakt 7 (Native Instruments)",
                            track_guid="{SCRATCH}")

    assert result == {"status": "unavailable", "parameters": [],
                      "reason": "instrument_no_defaults"}
    assert bridge.calls == 0


def test_probe_reports_cleanup_unsupported_and_keeps_no_parameters() -> None:
    bridge = FakeBridge(delete_supported=False)
    result = probe_defaults(FakeSession(bridge), "VST3: Saturn 2 (FabFilter)",
                            track_guid="{SCRATCH}")

    assert result["status"] == "failed"
    assert result["reason"] == "cleanup_unsupported"
    assert result["parameters"] == []
    assert [call[0] for call in bridge.calls] == [
        "fx.insert", "fx.list_params", "fx.delete"]


def test_probe_cleans_up_when_the_parameter_read_fails() -> None:
    bridge = FakeBridge(read_error=RuntimeError("listing failed"))
    result = probe_defaults(FakeSession(bridge), "VST3: Saturn 2 (FabFilter)",
                            track_guid="{SCRATCH}")

    assert result["status"] == "failed"
    assert result["reason"] == "parameter_read_failed"
    assert [call[0] for call in bridge.calls] == [
        "fx.insert", "fx.list_params", "fx.delete"]
    assert bridge.calls[2][1]["fx_guid"] == "{PROBE_FX_1}"


def test_probe_maps_a_read_timeout_without_raising() -> None:
    bridge = FakeBridge(read_error=TimeoutError("slow read"))
    result = probe_defaults(FakeSession(bridge), "VST3: Saturn 2 (FabFilter)",
                            track_guid="{SCRATCH}", timeout_s=5.0)

    assert result["status"] == "failed"
    assert result["reason"] == "timeout"
    assert bridge.calls[-1][0] == "fx.delete"


def test_probe_reports_insert_failure_without_a_cleanup_attempt() -> None:
    class InsertFails(FakeBridge):
        def call(self, operation, arguments, timeout_s=10.0):
            self.calls.append((operation, dict(arguments), timeout_s))
            if operation == "fx.insert":
                raise RuntimeError("plugin not installed")
            return super().call(operation, arguments, timeout_s=timeout_s)

    bridge = InsertFails()
    result = probe_defaults(FakeSession(bridge), "VST: Missing",
                            track_guid="{SCRATCH}")

    assert result["status"] == "failed"
    assert result["reason"] == "insert_failed"
    assert [call[0] for call in bridge.calls] == ["fx.insert"]


def test_probe_rejects_bad_arguments_without_touching_the_bridge() -> None:
    bridge = ExplodingBridge()
    session = FakeSession(bridge)
    assert probe_defaults(session, "VST: X", track_guid="")["reason"] == \
        "invalid_track_guid"
    assert probe_defaults(session, "VST: X", track_guid="{T}", slot=-2)["reason"] == \
        "invalid_slot"
    assert probe_defaults(session, "VST: X", track_guid="{T}", timeout_s=0)["reason"] == \
        "invalid_timeout"
    assert bridge.calls == 0


class FakeApplySession:
    """Transaction-shaped session: insert/delete go through plan/apply."""

    def __init__(self) -> None:
        self.bridge = FakeBridge()
        self.plans: list[tuple[list, str]] = []
        self.applies: list[tuple[dict, bool]] = []

    def plan(self, operations, *, reason: str):
        self.plans.append((list(operations), reason))
        return {"operations": tuple(operations), "reason": reason}

    def apply(self, plan, *, dry_run: bool):
        self.applies.append((plan, dry_run))
        operation = plan["operations"][0]
        if operation.kind == "fx.insert":
            assert dry_run is False
            return {"operation_results": [{"changed": True, "fx_guid": "{TX_FX}"}]}
        assert operation.kind == "fx.delete"
        return {"operation_results": [{"changed": True,
                                       "deleted_fx_guid": operation.values["fx_guid"]}]}


def test_probe_uses_the_transaction_layer_when_a_session_offers_it() -> None:
    session = FakeApplySession()
    result = probe_defaults(session, "VST3: Pro-Q 4 (FabFilter)",
                            track_guid="{SCRATCH}", slot=-1)

    assert result["status"] == "ok"
    assert [plan[0][0].kind for plan in session.plans] == ["fx.insert", "fx.delete"]
    insert_op = session.plans[0][0][0]
    assert insert_op.values == {"name": "VST3: Pro-Q 4 (FabFilter)"}
    delete_op = session.plans[1][0][0]
    assert delete_op.values == {"fx_guid": "{TX_FX}"}
    assert [dry_run for _, dry_run in session.applies] == [False, False]
    assert [call[0] for call in session.bridge.calls] == ["fx.list_params"]


def test_probe_refuses_a_cleanup_that_names_a_different_instance() -> None:
    class WrongDelete(FakeBridge):
        def call(self, operation, arguments, timeout_s=10.0):
            response = super().call(operation, arguments, timeout_s=timeout_s)
            if operation == "fx.delete":
                response["deleted_fx_guid"] = "{SOMEONE_ELSE}"
            return response

    result = probe_defaults(FakeSession(WrongDelete()), "VST: X",
                            track_guid="{SCRATCH}")
    assert result["status"] == "failed"
    assert result["reason"] == "cleanup_unsupported"


def test_cleanup_grace_is_bounded() -> None:
    # The constant exists so cleanup cannot inherit an expired main deadline.
    assert 0 < CLEANUP_GRACE_S <= 10.0
