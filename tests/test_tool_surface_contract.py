"""The MCP tool surface, its map and the model README must be one fact.

The numbers are never duplicated by hand here: the test reads the live server
registry, the logical ``TOOL_SURFACE`` map and the README table, and compares
the three sets.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from reaper_audio import mcp_server

README = Path(__file__).resolve().parents[1] / "docs" / "MCP-README-LLM.md"

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _registered_tools() -> set[str]:
    return set(mcp_server.server._tool_manager._tools)


def _mapped_tools() -> list[str]:
    return [tool for group in mcp_server.TOOL_SURFACE.values()
            for tool in group["tools"]]


def _documented_tools() -> set[str]:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Tool map")
    end = text.index("The official API catalog", start)
    return set(re.findall(r"^\| `([^`]+)`", text[start:end], re.M))


def test_registry_map_and_readme_name_the_same_tools() -> None:
    registered = _registered_tools()
    mapped = _mapped_tools()
    documented = _documented_tools()

    assert set(mapped) == registered, (
        f"missing from the map: {sorted(registered - set(mapped))}; "
        f"stale in the map: {sorted(set(mapped) - registered)}")
    assert len(mapped) == len(set(mapped)), "a tool is listed in two groups"
    assert documented == registered, (
        f"missing from the README: {sorted(registered - documented)}; "
        f"stale in the README: {sorted(documented - registered)}")


def test_every_group_has_a_purpose_and_context() -> None:
    for name, group in mcp_server.TOOL_SURFACE.items():
        assert group.get("purpose"), name
        assert group.get("use_when"), name
        assert group.get("context"), name
        assert group.get("not_for"), name
        for tool in group["tools"]:
            assert tool in _registered_tools(), (name, tool)


def test_readme_count_matches_the_live_registry() -> None:
    text = README.read_text(encoding="utf-8")
    match = re.search(r"registers (\d+) tools and (\w+) resource", text)
    assert match, "the README must state the live tool/resource counts"
    stated_tools = int(match.group(1))
    stated_templates = _WORDS.get(match.group(2).lower())
    live_tools = len(_registered_tools())
    live_templates = len(mcp_server.server._resource_manager._templates)
    assert stated_tools == live_tools
    assert stated_templates == live_templates


def test_audio_delta_lives_inside_mix_evidence_not_a_new_tool() -> None:
    parameters = inspect.signature(mcp_server.reaper_mix_evidence).parameters
    assert "compare_analysis_id" in parameters
    assert "reaper_analysis_delta" not in _registered_tools()


def test_new_mode_arguments_are_strict_schema_enums() -> None:
    tools = mcp_server.server._tool_manager._tools
    panorama = tools["reaper_panorama_open"].parameters["properties"]["mode"]
    assert panorama["enum"] == ["auto", "pages", "single"]
    assert panorama["default"] == "auto"
    capabilities = tools["reaper_capabilities"].parameters["properties"]
    assert capabilities["mode"]["enum"] == ["summary", "domain", "tools"]
    assert capabilities["detail"]["enum"] == ["names", "summary", "full"]


def test_every_registered_tool_has_a_typed_argument_schema() -> None:
    for name, tool in mcp_server.server._tool_manager._tools.items():
        schema = tool.parameters
        assert schema.get("type") == "object", name
        assert isinstance(schema.get("properties"), dict), name


def test_no_new_tool_per_metric_or_model_backend() -> None:
    registered = _registered_tools()
    # Optional model backends stay behind the existing status/describe/ask and
    # spatial families; a new backend must not add a tool name here.
    model_prefixes = ("reaper_flamingo_", "reaper_spatial_")
    for name in registered:
        if name.startswith(model_prefixes):
            assert name.rsplit("_", 1)[-1] in {"status", "describe", "ask", "analyze",
                                               "timeline"}, name


def test_server_instructions_delegate_to_the_handbook() -> None:
    text = mcp_server.server.instructions or ""
    # The short instruction must point at the handbook and gate the work.
    assert "reaper://handbook" in text
    assert "HANDBOOK_REQUIRED" in text
    assert "no invented thresholds" in text
    assert "status" in text and "measure" in text and "edit" in text
    assert len(text) <= 2200

    # The complete protocol lives in the handbook, not in the injected text.
    handbook = mcp_server.handbook_resource()
    assert "size-independent" in handbook
    assert "every declared page" in handbook
    assert "structural token" in handbook
    assert "work mode" in handbook.lower()
    assert "manifest_sha256" in handbook
    assert "anti-patterns" in handbook.lower()


def test_model_guide_resource_carries_the_same_contract() -> None:
    assert "reaper://guide/model" in mcp_server.server._resource_manager._resources
    guide = json.loads(mcp_server.model_guide_resource())
    assert guide["project_context"]["start"].startswith("reaper_panorama_open")
    assert "pages are the complete path" in guide["project_context"]["pages"]
    assert "manifest_sha256" in guide["project_context"]["ack"]
    assert "change_page" in guide["updates"]["paged"]
    assert "stable object ids" in guide["updates"]["identity"]
    assert "metric_table" in guide["updates"]
    assert "No better/worse" in guide["updates"]["comparability"]
    assert guide["scoped_analysis"]["catalog"] == \
        "reaper_capabilities(mode='tools').analyzers"
    assert "not an automatic next step" in guide["scoped_analysis"]["no_loop"]
    assert "result_page" in guide["exact_rows"]["rule"]
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    assert 'reaper://guide/model' in source


def test_analyzer_catalog_is_in_the_same_task_surface() -> None:
    capabilities = mcp_server.reaper_capabilities(mode="tools")
    assert capabilities["surface"] == "logical_task_groups"
    assert capabilities["analyzers"]
    assert "relative" in capabilities["analyzer_rule"]
    for name, row in capabilities["analyzers"].items():
        assert row["scope"] and row["prerequisites"]
        assert row["cost"]["class"] in {"light", "medium", "heavy"}


def test_readme_has_no_single_first_contradiction() -> None:
    text = README.read_text(encoding="utf-8")
    assert 'Prefer `mode="single"`' not in text
    assert "default for compatibility" not in text
    assert "size-independent" in text
    assert "change_page" in text


FORBIDDEN_MUTATION_KEYS = {
    "delta", "delta_id", "delta_hash", "mutation_delta",
    "project_before", "project_after",
    "from_revision", "to_revision",
    "revision_before", "revision_after",
    "base_revision", "target_revision", "base", "target",
}


def test_tool_return_literals_expose_no_internal_mutation_record() -> None:
    """Static guard: no tool returns a dict literal with an internal record key.

    Runtime checks cover the paths built through helpers; this scan makes the
    rule visible at the surface itself, so a new tool cannot reintroduce
    ``delta``/``project_before``/``revision_after`` in its own return value.
    """
    import ast
    from pathlib import Path as _Path

    source = _Path(mcp_server.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def is_tool(node: ast.FunctionDef) -> bool:
        return any("server.tool" in ast.unparse(item) for item in node.decorator_list)

    offenders: list[tuple[str, int, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not is_tool(node):
            continue
        for item in ast.walk(node):
            if not isinstance(item, ast.Return) or item.value is None:
                continue
            for literal in ast.walk(item.value):
                if not isinstance(literal, ast.Dict):
                    continue
                for key in literal.keys:
                    if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                            and key.value in FORBIDDEN_MUTATION_KEYS):
                        offenders.append((node.name, literal.lineno, key.value))
    assert offenders == []
