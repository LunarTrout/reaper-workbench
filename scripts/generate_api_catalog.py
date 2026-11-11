"""Generate a ReaScript signature catalog from Cockos' official API reference."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen


OFFICIAL_URL = "https://www.reaper.fm/sdk/reascript/reascripthelp.html"
DEFAULT_OUTPUT = (Path(__file__).resolve().parents[1] / "src" / "reaper_audio" /
                  "reaper" / "api_catalog.json")
HOST_HANDLE_TYPES = {
    "AudioAccessor", "HWND", "IReaperControlSurface", "KbdSectionInfo",
    "MediaItem", "MediaItem_Take", "MediaTrack", "PCM_source",
    "ProjectMarker", "ReaProject", "TrackEnvelope", "joystick_device",
}


class _FunctionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.last_anchor = ""
        self.current_name = ""
        self.current_kind: str | None = None
        self.current_parts: list[str] = []
        self.signatures: dict[str, dict[str, str]] = {}
        self.descriptions: dict[str, str] = {}
        self.description_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("name"):
            self._flush_description()
            self.last_anchor = values["name"] or ""
            self.description_parts = []
        if tag == "div":
            class_name = values.get("class", "")
            kind = {"c_func": "c", "l_func": "lua", "e_func": "eel",
                    "p_func": "python"}.get(class_name or "")
            if kind:
                self.current_name = self.last_anchor
                self.current_kind = kind
                self.current_parts = []
        elif tag in {"br", "p", "li", "tr"} and not self.current_kind:
            self.description_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.current_kind:
            self.current_parts.append(data)
        elif self.last_anchor:
            self.description_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.current_kind:
            text = " ".join(" ".join(self.current_parts).split())
            text = re.sub(r"^(?:C|Lua|EEL2|Python):\s*", "", text)
            if self.current_name and text:
                self.signatures.setdefault(self.current_name, {})[
                    self.current_kind] = text
            self.current_name = ""
            self.current_kind = None
            self.current_parts = []
        elif tag in {"p", "li", "tr"} and not self.current_kind:
            self.description_parts.append("\n")

    def _flush_description(self) -> None:
        if not self.last_anchor:
            return
        lines = []
        for raw_line in "".join(self.description_parts).splitlines():
            line = " ".join(raw_line.split())
            if line:
                lines.append(line)
        description = "\n".join(lines)
        if description:
            self.descriptions[self.last_anchor] = description


def _split_parameters(text: str) -> list[str]:
    if not text.strip() or text.strip() == "void":
        return []
    return [part.strip() for part in text.split(",")]


def _parse_c_signature(name: str, text: str) -> dict:
    match = re.fullmatch(r"\s*(.+?)\s+([A-Za-z_]\w*)\((.*)\)\s*", text)
    if not match or match.group(2) != name:
        return {"raw": text, "parsed": False}

    return_type, _, parameter_text = match.groups()
    params = []
    for position, raw in enumerate(_split_parameters(parameter_text)):
        param_match = re.fullmatch(r"(.+?)\s+([A-Za-z_]\w*)", raw)
        if param_match:
            type_name, param_name = param_match.groups()
        else:
            type_name, param_name = raw, f"arg{position}"
        base = type_name.replace("const", "").replace("*", "").strip()
        pointer = "*" in type_name
        params.append({"name": param_name, "type": type_name,
                       "host_handle": base in HOST_HANDLE_TYPES and pointer})

    base_return = return_type.replace("const", "").replace("*", "").strip()
    return_handle = base_return in HOST_HANDLE_TYPES and "*" in return_type
    return {"raw": text, "parsed": True, "return_type": return_type,
            "return_host_handle": return_handle, "parameters": params}


def _parse_lua_signature(name: str, text: str) -> dict:
    call = re.search(r"reaper\.([A-Za-z_]\w*)\((.*)\)\s*$", text)
    if not call or call.group(1) != name:
        return {"raw": text, "parsed": False, "parameters": [], "returns": []}

    call_start = call.start()
    prefix = text[:call_start].strip()
    if prefix.endswith("="):
        prefix = prefix[:-1].strip()
    if prefix.endswith("void"):
        prefix = ""

    def parse_cells(value: str, *, names: bool) -> list[dict[str, str | bool]]:
        if not value:
            return []
        cells = []
        for raw in value.split(","):
            tokens = raw.strip().split()
            optional = any(token.lower() == "optional" for token in tokens)
            tokens = [token for token in tokens if token.lower() != "optional"]
            if not tokens:
                continue
            type_name = tokens[0]
            item: dict[str, str | bool] = {
                "type": type_name,
                "host_handle": type_name in HOST_HANDLE_TYPES,
                "optional": optional,
            }
            if names:
                item["name"] = tokens[1] if len(tokens) > 1 else f"arg{len(cells)}"
            cells.append(item)
        return cells

    parameter_text = call.group(2).strip()
    return {"raw": text, "parsed": True,
            "returns": parse_cells(prefix, names=False),
            "parameters": parse_cells(parameter_text, names=True)}


def build_catalog(html: str) -> dict:
    parser = _FunctionParser()
    parser.feed(html)
    parser.close()
    parser._flush_description()
    functions = {}
    for name, signatures in parser.signatures.items():
        c_text = signatures.get("c")
        lua_text = signatures.get("lua")
        if not c_text or not lua_text:
            continue
        functions[name] = {"c": _parse_c_signature(name, c_text),
                           "lua": _parse_lua_signature(name, lua_text),
                           "eel": signatures.get("eel"),
                           "python": signatures.get("python"),
                           "description": parser.descriptions.get(name, "")}
    version = re.search(r"Generated by REAPER (v[0-9.]+)", html)
    source_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return {"source": OFFICIAL_URL,
            "documentation_version": version.group(1) if version else None,
            "source_sha256": source_hash,
            "function_count": len(functions),
            "functions": functions}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-file", type=Path,
                        help="Parse an already downloaded official HTML file")
    arguments = parser.parse_args()
    if arguments.source_file:
        html = arguments.source_file.read_text(encoding="utf-8", errors="replace")
    else:
        request = Request(OFFICIAL_URL, headers={"User-Agent": "reaper-workbench/0.1"})
        with urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
    catalog = build_catalog(html)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    output_text = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    arguments.output.write_text(output_text, encoding="utf-8", newline="\n")
    unparsed = [name for name, data in catalog["functions"].items()
                if not data["c"].get("parsed")]
    print(f"Wrote {catalog['function_count']} function signatures; "
          f"documentation {catalog['documentation_version']}; "
          f"unparsed C declarations: {len(unparsed)}")
    if unparsed:
        print("Unparsed examples: " + ", ".join(unparsed[:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
