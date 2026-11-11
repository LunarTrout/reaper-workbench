from __future__ import annotations

import pytest

from reaper_audio.errors import ValidationError
from reaper_audio.reaper.api_catalog import get_api_catalog


def test_catalog_includes_documented_reaper_handles_and_out_returns() -> None:
    catalog = get_api_catalog()
    get_track = catalog.get("GetTrack")
    item_from_point = catalog.get("GetItemFromPoint")

    assert len(catalog.functions) >= 700
    assert get_track.parameters[0].value_type.name == "ReaProject"
    assert get_track.parameters[0].value_type.is_host_handle
    assert get_track.returns[0].name == "MediaTrack"
    assert get_track.returns[0].is_host_handle
    assert [item.name for item in item_from_point.returns] == [
        "MediaItem", "MediaItem_Take",
    ]
    assert all(item.is_host_handle for item in item_from_point.returns)


def test_catalog_recovers_optional_parameters_from_official_c_signature() -> None:
    catalog = get_api_catalog()
    create_send = catalog.get("CreateTrackSend")
    popup = catalog.get("ShowPopupMenu")

    assert create_send.parameters[1].name == "desttrIn"
    assert create_send.parameters[1].value_type.is_host_handle
    assert create_send.parameters[1].value_type.optional
    assert all(parameter.value_type.optional for parameter in popup.parameters[3:])


def test_catalog_rejects_undocumented_function_name() -> None:
    with pytest.raises(ValidationError):
        get_api_catalog().get("not_a_reaper_api_function")


def test_catalog_search_finds_relevant_api_without_listing_everything() -> None:
    catalog = get_api_catalog()
    hits = catalog.search("split item at position", limit=5)

    assert len(hits) <= 5
    assert hits[0].signature.name == "SplitMediaItem"
    assert "right-hand split" in hits[0].signature.description


def test_catalog_search_expands_common_russian_api_terms() -> None:
    hits = get_api_catalog().search("разрезать item в позиции", limit=5)

    assert hits[0].signature.name == "SplitMediaItem"


@pytest.mark.parametrize(("object", "operation", "qualifiers", "expected"), [
    ("track", "set", ["D_VOL", "D_PAN"], "SetMediaTrackInfo_Value"),
    ("media_item_take", "set", ["D_STARTOFFS"],
     "SetMediaItemTakeInfo_Value"),
    ("track", "create", ["audio_accessor"], "CreateTrackAudioAccessor"),
])
def test_structured_search_prioritizes_object_operation_and_properties(
        object: str, operation: str, qualifiers: list[str], expected: str) -> None:
    hits = get_api_catalog().search_card(
        object, operation, qualifiers=qualifiers, limit=5, detail="names")

    assert hits[0].signature.name == expected


def test_catalog_search_has_compact_and_full_detail_levels() -> None:
    signature = get_api_catalog().get("Main_SaveProjectEx")
    compact = signature.to_dict(detail="summary")
    full = signature.to_dict(detail="full")
    names = signature.to_dict(detail="names")

    assert len(compact["description"]) <= 320
    assert "set as the new project filename" in full["description"]
    assert full["eel_signature"]
    assert full["python_signature"]
    assert set(names) == {"name"}


@pytest.mark.parametrize("query,limit", [("", 5), ("split", 0), ("split", 51)])
def test_catalog_search_rejects_invalid_query_or_limit(query: str, limit: int) -> None:
    with pytest.raises(ValidationError):
        get_api_catalog().search(query, limit=limit)
