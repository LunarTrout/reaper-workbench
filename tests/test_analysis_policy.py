from __future__ import annotations

import pytest

from reaper_audio.daw.analysis_policy import (
    MAX_ANALYSIS_WORKERS,
    resolve_analysis_workers,
    validate_analysis_worker_request,
)
from reaper_audio.daw.full_capture import _analysis_worker_count
from reaper_audio.daw.session import _bundle_analysis_workers
from reaper_audio.errors import ValidationError
from reaper_audio.models import Section

GB = 1024 ** 3


def test_empty_input_has_no_pool() -> None:
    plan = resolve_analysis_workers(asset_count=0, window_s=20.0,
                                    available_memory_bytes=32 * GB)
    assert plan.workers == 0
    assert plan.reason == "no_assets"


def test_short_window_uses_up_to_eight() -> None:
    plan = resolve_analysis_workers(asset_count=43, window_s=20.0,
                                    available_memory_bytes=32 * GB)
    assert plan.workers == 8
    assert plan.window_class == "short"
    assert plan.reason == "short_window_default"
    assert resolve_analysis_workers(asset_count=2, window_s=20.0,
                                    available_memory_bytes=32 * GB).workers == 2


def test_full_and_unknown_duration_use_four() -> None:
    full = resolve_analysis_workers(asset_count=43, window_s=300.0,
                                    available_memory_bytes=32 * GB)
    unknown = resolve_analysis_workers(asset_count=43, window_s=None,
                                       available_memory_bytes=32 * GB)
    assert full.workers == 4
    assert full.window_class == "full"
    assert full.reason == "long_window_default"
    assert unknown.workers == 4
    assert unknown.window_class == "unknown"
    assert unknown.reason == "unknown_window_default"


def test_low_memory_lowers_the_default() -> None:
    plan = resolve_analysis_workers(asset_count=43, window_s=20.0,
                                    available_memory_bytes=2 * GB)
    assert plan.workers == 2
    assert plan.memory_limit_workers == 2

    with pytest.raises(ValidationError, match="Available memory cannot support"):
        resolve_analysis_workers(asset_count=43, window_s=300.0,
                                 available_memory_bytes=4 * GB)


def test_explicit_override_is_safe_and_capped() -> None:
    assert resolve_analysis_workers(
        asset_count=43, window_s=20.0, requested=6,
        available_memory_bytes=32 * GB).workers == 6
    with pytest.raises(ValidationError, match="analysis_workers must be between"):
        validate_analysis_worker_request(MAX_ANALYSIS_WORKERS + 1)
    with pytest.raises(ValidationError, match="analysis_workers must be between"):
        validate_analysis_worker_request(0)
    with pytest.raises(ValidationError, match="analysis_workers must be between"):
        validate_analysis_worker_request(True)


def test_explicit_eight_is_rejected_for_full_capture() -> None:
    with pytest.raises(ValidationError, match="memory-safe limit") as excinfo:
        resolve_analysis_workers(
            asset_count=43, window_s=300.0, requested=8,
            available_memory_bytes=32 * GB)
    assert excinfo.value.details["max_safe_workers"] == 5
    assert excinfo.value.details["requested"] == 8


def test_explicit_eight_is_allowed_for_short_capture() -> None:
    plan = resolve_analysis_workers(
        asset_count=43, window_s=20.0, requested=8,
        available_memory_bytes=32 * GB)
    assert plan.workers == 8
    assert plan.reason == "explicit_override"


def test_no_selector_uses_payload_count_as_worker_count() -> None:
    # Both production paths must cap a 100-asset full capture at four.
    assert _analysis_worker_count(None, 100, window_s=300.0,
                                  available_memory_bytes=32 * GB) == 4
    assert _bundle_analysis_workers(
        100, section=None,
        time_window={"project_start_s": 0.0, "project_end_s": 300.0},
        available_memory_bytes=32 * GB) == 4


def test_both_production_paths_use_the_same_policy() -> None:
    assert _analysis_worker_count(None, 43, window_s=20.0,
                                  available_memory_bytes=32 * GB) == 8
    assert _bundle_analysis_workers(
        43, section=Section(0.0, 20.0), time_window=None,
        available_memory_bytes=32 * GB) == 8
    assert _analysis_worker_count(None, 43, window_s=300.0,
                                  available_memory_bytes=32 * GB) == 4
    assert _bundle_analysis_workers(
        43, section=None,
        time_window={"project_start_s": 0.0, "project_end_s": 300.0},
        available_memory_bytes=32 * GB) == 4
