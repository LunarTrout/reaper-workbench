"""Typed view of the documented ReaScript API catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from math import log

from ..errors import ValidationError


@dataclass(frozen=True, slots=True)
class ApiValueType:
    name: str
    is_host_handle: bool
    optional: bool = False


@dataclass(frozen=True, slots=True)
class ApiParameter:
    name: str
    value_type: ApiValueType


@dataclass(frozen=True, slots=True)
class ApiSignature:
    name: str
    parameters: tuple[ApiParameter, ...]
    returns: tuple[ApiValueType, ...]
    raw_lua_signature: str
    description: str = ""
    raw_c_signature: str = ""
    raw_eel_signature: str | None = None
    raw_python_signature: str | None = None

    def to_dict(self, *, detail: str = "summary", score: float | None = None) -> dict:
        if detail not in {"names", "summary", "full"}:
            raise ValidationError("detail must be 'names', 'summary', or 'full'")
        result: dict = {"name": self.name}
        if score is not None:
            result["score"] = round(score, 4)
        if detail == "names":
            return result
        result.update({
            "description": _description_snippet(self.description),
            "lua_signature": self.raw_lua_signature,
            "parameters": [
                {"name": item.name, "type": item.value_type.name,
                 "host_handle": item.value_type.is_host_handle,
                 "optional": item.value_type.optional}
                for item in self.parameters
            ],
            "returns": [
                {"type": item.name, "host_handle": item.is_host_handle,
                 "optional": item.optional}
                for item in self.returns
            ],
        })
        if detail == "full":
            result.update({"description": self.description,
                           "c_signature": self.raw_c_signature,
                           "eel_signature": self.raw_eel_signature,
                           "python_signature": self.raw_python_signature})
        return result


@dataclass(frozen=True, slots=True)
class ApiSearchHit:
    signature: ApiSignature
    score: float

    def to_dict(self, *, detail: str = "summary") -> dict:
        return self.signature.to_dict(detail=detail, score=self.score)


@dataclass(frozen=True, slots=True)
class ApiCallResult:
    name: str
    returns: tuple[object, ...]
    arrays: dict[int, tuple[float, ...]]
    project_before: dict | None = None
    project_after: dict | None = None


@dataclass(frozen=True, slots=True)
class ApiCatalog:
    documentation_version: str | None
    functions: dict[str, ApiSignature]

    def get(self, name: str) -> ApiSignature:
        try:
            return self.functions[name]
        except KeyError as exc:
            raise ValidationError(
                "API function is not in the bundled official signature catalog",
                details={"name": name,
                         "catalog_version": self.documentation_version},
            ) from exc

    def search(self, query: str, *, limit: int = 8,
               detail: str = "summary") -> tuple[ApiSearchHit, ...]:
        """Search documented API functions without returning the whole catalog.

        Scoring is a small field-weighted BM25 index over official function
        names, parameter names/types, and help text. A compact Russian-to-API
        vocabulary expands common object and operation words; it does not encode
        complete workflows or prescribe when an operation should be used.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValidationError("API search query must be a non-empty string")
        term_weights = {term: 1.0 for term in _query_terms(query)}
        return self._rank(term_weights, limit=limit, detail=detail,
                          exact_query=query)

    def search_card(self, object: str, operation: str, *,
                    qualifiers: list[str] | tuple[str, ...] = (),
                    limit: int = 8,
                    detail: str = "summary") -> tuple[ApiSearchHit, ...]:
        """Search from the MCP object/operation/qualifier contract.

        The object and operation are stronger signals than qualifiers. This
        keeps a broad qualifier such as ``audio_accessor`` from outranking the
        operation ``create`` and object ``track`` with unrelated accessor
        lifecycle functions.
        """
        if not isinstance(object, str) or not object.strip() or len(object) > 80:
            raise ValidationError("object must be 1 to 80 characters")
        if (not isinstance(operation, str) or not operation.strip() or
                len(operation) > 80):
            raise ValidationError("operation must be 1 to 80 characters")
        if (not isinstance(qualifiers, (list, tuple)) or len(qualifiers) > 8 or
                any(not isinstance(item, str) or not item.strip() or len(item) > 80
                    for item in qualifiers)):
            raise ValidationError("qualifiers must contain at most 8 short strings")
        term_weights: dict[str, float] = {}
        object_terms = _query_terms(object)
        operation_terms = _query_terms(operation)
        qualifier_terms = [term for item in qualifiers
                           for term in _query_terms(item)]
        for text, weight in ((object_terms, 2.0), (operation_terms, 2.5),
                             *((_query_terms(item), 1.0) for item in qualifiers)):
            for term in text:
                term_weights[term] = max(term_weights.get(term, 0.0), weight)
        return self._rank(term_weights, limit=limit, detail=detail,
                          role_terms=(object_terms, operation_terms,
                                      qualifier_terms))

    def _rank(self, term_weights: dict[str, float], *, limit: int,
              detail: str, exact_query: str | None = None,
              role_terms: tuple[list[str], list[str], list[str]] | None = None
              ) -> tuple[ApiSearchHit, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValidationError("API search limit must be an integer from 1 to 50")
        if detail not in {"names", "summary", "full"}:
            raise ValidationError("detail must be 'names', 'summary', or 'full'")
        if not term_weights:
            raise ValidationError("API search query contains no searchable terms")

        fields_by_name = {name: _signature_fields(signature)
                          for name, signature in self.functions.items()}
        document_frequency: dict[str, int] = {}
        field_lengths = {field_name: [] for field_name in _FIELD_WEIGHTS}
        counters: dict[str, dict[str, dict[str, int]]] = {}
        for name, fields in fields_by_name.items():
            counters[name] = {}
            present: set[str] = set()
            for field_name, text in fields.items():
                counts = _token_counts(text)
                counters[name][field_name] = counts
                field_lengths[field_name].append(sum(counts.values()))
                present.update(counts)
            for term in present:
                document_frequency[term] = document_frequency.get(term, 0) + 1

        document_count = max(1, len(self.functions))
        average_lengths = {
            field_name: (sum(lengths) / len(lengths) if lengths else 1.0)
            for field_name, lengths in field_lengths.items()
        }
        ranked: list[ApiSearchHit] = []
        for name, signature in self.functions.items():
            score = 0.0
            for term, query_weight in term_weights.items():
                frequency = document_frequency.get(term, 0)
                if not frequency:
                    continue
                inverse_frequency = log(
                    1.0 + (document_count - frequency + 0.5) / (frequency + 0.5)
                )
                for field_name, weight in _FIELD_WEIGHTS.items():
                    term_frequency = counters[name][field_name].get(term, 0)
                    if not term_frequency:
                        continue
                    length = max(1, sum(counters[name][field_name].values()))
                    average = max(1.0, average_lengths[field_name])
                    normalization = term_frequency + _BM25_K1 * (
                        1.0 - _BM25_B + _BM25_B * length / average
                    )
                    score += query_weight * weight * inverse_frequency * (
                        term_frequency * (_BM25_K1 + 1.0) / normalization
                    )
            if score > 0 and role_terms is not None:
                name_tokens = counters[name]["name"]
                parameter_tokens = counters[name]["parameters"]
                description_tokens = counters[name]["description"]

                def coverage(terms: list[str], weights: dict[str, float]) -> float:
                    distinct = set(terms)
                    if not distinct:
                        return 1.0
                    matched = 0.0
                    for term in distinct:
                        matched += max(
                            (weight for field_tokens, weight in (
                                (name_tokens, weights["name"]),
                                (parameter_tokens, weights["parameters"]),
                                (description_tokens, weights["description"]),
                            ) if term in field_tokens),
                            default=0.0,
                        )
                    return matched / len(distinct)

                object_coverage = coverage(
                    role_terms[0], {"name": 1.0, "parameters": 0.65,
                                    "description": 0.15})
                operation_coverage = coverage(
                    role_terms[1], {"name": 1.0, "parameters": 0.2,
                                    "description": 0.05})
                qualifier_coverage = coverage(
                    role_terms[2], {"name": 0.85, "parameters": 1.0,
                                    "description": 0.55})
                score *= ((0.12 + 0.88 * object_coverage) *
                          (0.08 + 0.92 * operation_coverage) *
                          (0.05 + 0.95 * qualifier_coverage))
            if score > 0:
                ranked.append(ApiSearchHit(signature=signature, score=score))

        if exact_query is not None:
            lowered = re.sub(r"\s+", "", exact_query).casefold()
            ranked = [
                ApiSearchHit(hit.signature,
                             hit.score + 1000.0
                             if lowered == hit.signature.name.casefold()
                             else hit.score)
                for hit in ranked
            ]
        ranked.sort(key=lambda hit: (-hit.score, hit.signature.name.casefold()))
        return tuple(ranked[:limit])


_FIELD_WEIGHTS = {"name": 5.0, "parameters": 2.8, "description": 1.4,
                  "signature": 0.5}
_BM25_K1 = 1.5
_BM25_B = 0.72
_RUSSIAN_API_TERMS = {
    "дорожка": "track", "дорожки": "track", "дорожку": "track",
    "дорожке": "track", "трек": "track", "трека": "track",
    "треки": "track", "клип": "item", "клипа": "item", "клипы": "item",
    "айтем": "item", "айтема": "item", "айтемы": "item",
    "разрезать": "split", "разрез": "split", "нарезать": "split",
    "разделить": "split", "переместить": "move", "перемещение": "move",
    "сдвинуть": "move", "позиция": "position", "позиции": "position",
    "положение": "position", "эффект": "fx", "эффекты": "fx",
    "плагин": "fx", "плагины": "fx", "параметр": "parameter",
    "параметры": "parameter", "проект": "project", "проекта": "project",
    "сохранить": "save", "громкость": "volume", "панорама": "pan",
    "темп": "tempo", "огибающая": "envelope", "маркер": "marker",
    "регион": "region", "шина": "track", "послать": "send",
    "отправка": "send", "создать": "create", "добавить": "add",
    "удалить": "delete", "изменить": "set", "получить": "get",
    "найти": "find", "поиск": "search", "открыть": "open",
    "закрыть": "close", "включить": "enable", "выключить": "disable",
}
_API_TERM_ALIASES = {
    "vol": ("volume",), "volume": ("vol",),
    "startoffs": ("start", "offset"),
}


def _description_snippet(description: str, max_chars: int = 320) -> str:
    flattened = " ".join(description.split())
    if len(flattened) <= max_chars:
        return flattened
    return flattened[:max_chars - 1].rstrip() + "…"


def _token_counts(text: str) -> dict[str, int]:
    split_camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    counts: dict[str, int] = {}
    for token in re.findall(r"[^\W_]+", split_camel.casefold(), flags=re.UNICODE):
        if len(token) < 2:
            continue
        counts[token] = counts.get(token, 0) + 1
    return counts


def _query_terms(query: str) -> list[str]:
    tokens = list(_token_counts(query))
    expanded = tokens.copy()
    for token in tokens:
        translated = _RUSSIAN_API_TERMS.get(token)
        if translated:
            expanded.extend(_token_counts(translated))
        expanded.extend(_API_TERM_ALIASES.get(token, ()))
    return expanded


def _signature_fields(signature: ApiSignature) -> dict[str, str]:
    parameter_text = " ".join(
        f"{item.name} {item.value_type.name}" for item in signature.parameters
    )
    return {
        "name": signature.name,
        "parameters": parameter_text,
        "description": signature.description,
        "signature": " ".join((signature.raw_lua_signature,
                                signature.raw_c_signature,
                                signature.raw_eel_signature or "",
                                signature.raw_python_signature or "")),
    }


@lru_cache(maxsize=1)
def get_api_catalog() -> ApiCatalog:
    resource = files("reaper_audio.reaper").joinpath("api_catalog.json")
    try:
        with resource.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("Bundled REAPER API catalog cannot be loaded",
                              details={"reason": str(exc)}) from exc

    functions: dict[str, ApiSignature] = {}
    for name, entry in raw.get("functions", {}).items():
        lua = entry.get("lua", {})
        if not lua.get("parsed"):
            continue
        lua_parameters = lua.get("parameters", [])
        c_parameters = entry.get("c", {}).get("parameters", [])
        params = tuple(
            ApiParameter(
                str(parameter.get("name", "")),
                ApiValueType(
                    str(parameter["type"]),
                    bool(parameter.get("host_handle", False)),
                    bool(parameter.get("optional", False)) or (
                        index < len(c_parameters) and
                        str(c_parameters[index].get("name", "")).endswith("Optional")
                    ),
                ),
            )
            for index, parameter in enumerate(lua_parameters)
        )
        returns = tuple(
            ApiValueType(str(value["type"]), bool(value.get("host_handle", False)),
                         bool(value.get("optional", False)))
            for value in lua.get("returns", [])
        )
        functions[name] = ApiSignature(
            name=name,
            parameters=params,
            returns=returns,
            raw_lua_signature=str(lua.get("raw", "")),
            description=str(entry.get("description", "")),
            raw_c_signature=str(entry.get("c", {}).get("raw", "")),
            raw_eel_signature=entry.get("eel"),
            raw_python_signature=entry.get("python"),
        )
    return ApiCatalog(documentation_version=raw.get("documentation_version"),
                      functions=functions)
