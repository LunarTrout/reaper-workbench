"""Measured lossless compaction regressions.

The numbers are UTF-8 byte lengths of canonical JSON (``sort_keys``,
``separators=(",", ":")``) or of the server's indented delivered rendering,
measured on deterministic fixtures.  They prove the page and delta projections
do not lose values and that the dictionary/columnar forms actually reduce the
transported payload.  They are not token counts: no tokenizer is run here.
"""

from __future__ import annotations

import copy
import json

from reaper_audio.context.lifecycle import (_delivered_bytes, _diff_values,
                                            _merge_view_pages, _pack_view_sections,
                                            stable_fact_view)
from reaper_audio.context.panorama_view import build_view, canonical
from reaper_audio.context.semantic_delta import build_semantic_delta

from test_panorama_pages import _archive

#: Canonical page-payload sums measured before continuation-head dedup.
_BASELINE_PAGE_SUM = {42: 41_872, 101: 76_999}


def test_view_pages_are_lossless_and_101_source_pages_are_smaller(tmp_path) -> None:
    for sources in (42, 101):
        case = tmp_path / f"sources-{sources}"
        case.mkdir()
        archive, record, lifecycle = _archive(case, sources=sources)
        opened = lifecycle.open_view(record.project_id)
        pages = [opened] + [
            lifecycle.view_page(opened["handle"], opened["revision"], page_id)
            for page_id in opened["manifest"]["page_ids"][1:]
        ]
        merged = dict(_merge_view_pages(pages))
        view = build_view(lifecycle._document(record.capture_id), lean=False)
        rebuilt = {key: value for key, value in merged.items() if key != "view_sha256"}
        assert rebuilt == {key: value for key, value in view.items()
                           if key != "view_sha256"}
        total = sum(len(canonical(page)) for page in pages)
        if sources == 101:
            assert total <= _BASELINE_PAGE_SUM[101], total
        assert max(_delivered_bytes(page) for page in pages) <= 46_000
        assert _delivered_bytes(opened["manifest"]) < 3_000


def test_large_nested_view_is_exact_under_a_fragment_split() -> None:
    view = {"schema_version": "x", "huge": "данные" * 30_000,
            "nested": {"rows": [{"i": index, "payload": "x" * 2_000}
                                for index in range(20)]}}
    packed = _pack_view_sections(view, payload_budget=8_000, max_page_bytes=12_000)
    pages = [{"payload": dict(page)} for page in packed]
    assert len(pages) == 130
    assert max(_delivered_bytes(page) for page in pages) <= 12_000
    assert canonical(dict(_merge_view_pages(pages))) == canonical(view)


def test_metric_table_is_smaller_than_repeated_positional_ops(tmp_path) -> None:
    archive, record, lifecycle = _archive(tmp_path, sources=101)
    document = lifecycle._document(record.capture_id)
    target = copy.deepcopy(document)
    for row in target["analysis"]:
        row["metrics"] = {name: (value + 1.0 if isinstance(value, (int, float)) else value)
                          for name, value in (row.get("metrics") or {}).items()}
    delta = build_semantic_delta(document, target)
    table = delta["metric_table"]
    assert table["count"] == 101 * 24
    table_bytes = len(json.dumps(table, sort_keys=True, separators=(",", ":")))
    old_ops = _diff_values(stable_fact_view(document), stable_fact_view(target))
    old_bytes = len(json.dumps(old_ops, sort_keys=True, separators=(",", ":")))
    assert table_bytes < old_bytes, (table_bytes, old_bytes)

    # One row reconstructs the exact semantic op it represents.
    row = table["rows"][0]
    dictionaries = table["dictionaries"]
    source = dictionaries["source_ids"][row[0]]
    metric = dictionaries["metrics"][row[1]]
    assert table["path_pattern"].format(source_id=source, metric=metric) == \
        f"/analysis/{source}/metrics/{metric}"
    assert len(table["columns"]) == len(row)
    assert dictionaries["units"][row[2]] in {"dBFS", "LUFS", "dB", "dBTP", "Hz",
                                             "ratio", "count", "events/s",
                                             "events", "samples", "%", "LU", "BPM",
                                             "correlation", "FS", None}
    by_source = {row["source_id"]: row for row in document["analysis"]}
    target_by_source = {row["source_id"]: row for row in target["analysis"]}
    for table_row in table["rows"]:
        row_source = dictionaries["source_ids"][table_row[0]]
        row_metric = dictionaries["metrics"][table_row[1]]
        assert table_row[3] == by_source[row_source]["metrics"][row_metric]
        assert table_row[4] == target_by_source[row_source]["metrics"][row_metric]
        assert table_row[2] in range(len(dictionaries["units"]))
        assert table_row[5] in range(len(dictionaries["comparability"]))
        assert table_row[6] in range(len(dictionaries["reasons"]))


def test_delta_metric_projection_has_no_positional_identity(tmp_path) -> None:
    archive, record, lifecycle = _archive(tmp_path, sources=42)
    document = lifecycle._document(record.capture_id)
    target = copy.deepcopy(document)
    target["tracks"].insert(0, {"stable_id": "track:{NEW}", "guid": "{NEW}",
                                "index": 0, "mixer": {"volume": 1.0, "pan": 0.0}})
    for index, row in enumerate(target["tracks"][1:]):
        row["index"] = index + 1
    delta = build_semantic_delta(document, target)
    paths = {op["path"] for op in delta["ops"]}
    assert "/tracks/track:{NEW}" in paths
    assert not any(path.startswith("/tracks/0") for path in paths)
    assert "track:{NEW}" in delta["object_summary"]["track"]["added"]
