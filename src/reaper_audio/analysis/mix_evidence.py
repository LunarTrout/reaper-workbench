"""Evidence for mix decisions — measurements and questions, not recipes.

The agent receives this alongside the rendered audio and the reference
corridors, then decides which processing (gain, pan, EQ, compression,
saturation, amp/FX choice) serves the stated musical goal. Nothing here picks
a plugin or a parameter value.
"""
from __future__ import annotations

from typing import Any, Mapping


def _value(source: Mapping, name: str) -> float | None:
    cell = (source.get("metrics") or {}).get(name)
    if isinstance(cell, dict):
        value = cell.get("value")
    else:
        value = getattr(cell, "value", cell)
    return float(value) if isinstance(value, (int, float)) else None


def _name(sources: Mapping, key: str) -> str:
    source = sources.get(key) or {}
    name = (source.get("track_context") or {}).get("name")
    if name:
        return str(name)
    for candidate in sources.values():
        context = candidate.get("track_context") or {}
        if context.get("guid") == key:
            return str(context.get("name") or key)
    return str(key)


def build_mix_evidence(report: Mapping, *, masking_db: float = 6.0,
                       reference_sigma: float = 2.0,
                       max_issues: int = 40) -> dict:
    """Collect mix-relevant evidence with severity, never with a fixed action."""
    if not isinstance(max_issues, int) or max_issues < 1:
        raise ValueError("max_issues must be a positive integer")
    sources = report.get("sources") or {}
    issues: list[dict] = []

    masking = (report.get("masking_evidence") or {}).get("pairs", [])
    for pair in masking:
        db = abs(float(pair.get("db", 0.0)))
        if db < masking_db:
            continue
        issues.append({
            "id": "band_dominance",
            "severity": round(min(3.0, db / masking_db), 2),
            "sources": [_name(sources, pair.get("masker")),
                        _name(sources, pair.get("target"))],
            "evidence": {"band_hz": pair.get("band_hz"), "difference_db": db,
                         "cells": int(pair.get("cells") or 0)},
            "question": ("Which source should own this band for the chosen "
                         "goal, and is the answer level, pan, EQ or a different "
                         "source arrangement?"),
        })

    corridors = report.get("reference_corridors") or {}
    for source_id, corridor in corridors.items():
        rows = (corridor or {}).get("rows") or {}
        for metric, row in rows.items():
            delta = row.get("delta")
            mad = row.get("mad")
            if not isinstance(delta, (int, float)):
                continue
            unit = row.get("unit")
            spread = max(float(mad or 0.0), 0.5 if unit == "dB" else 1e-6)
            sigma = abs(float(delta)) / spread
            if sigma < reference_sigma:
                continue
            issues.append({
                "id": "reference_delta",
                "severity": round(min(3.0, sigma / reference_sigma), 2),
                "sources": [_name(sources, source_id)],
                "evidence": {"metric": metric, "mine": row.get("mine"),
                             "reference_median": row.get("median"),
                             "delta": delta, "mad": mad, "unit": unit},
                "question": ("Is this deviation part of the intended character "
                             "or should it be changed toward the reference?"),
            })

    partial = report.get("partial_loudness")
    if isinstance(partial, Mapping):
        for source_id, item in (partial.get("sources") or {}).items():
            share = (item.get("summary") or {}).get("share_of_partial_loudness_pct")
            if not isinstance(share, (int, float)):
                continue
            if share < 5.0 or share > 45.0:
                issues.append({
                    "id": "partial_audibility",
                    "severity": round(min(3.0, abs(share - 20.0) / 10.0), 2),
                    "sources": [_name(sources, source_id)],
                    "evidence": {"share_of_partial_loudness_pct": share},
                    "question": ("Is this source too exposed or too buried for "
                                 "the section, and what should mask it?"),
                })

    phase = report.get("pairwise_phase")
    if isinstance(phase, Mapping):
        for pair_key, item in (phase.get("pairs") or {}).items():
            correlation = item.get("correlation")
            if isinstance(correlation, (int, float)) and correlation < 0.2:
                issues.append({
                    "id": "phase",
                    "severity": round(min(3.0, (0.2 - correlation) / 0.2), 2),
                    "sources": list(item.get("sources") or [pair_key]),
                    "evidence": {"correlation": correlation},
                    "question": ("Is the low correlation intentional width or a "
                                 "phase problem that needs polarity/delay/pan?"),
                })

    # Many pair-wise measurements describe the same conflict. Keep one row per
    # (issue, sources, band/metric), then cap the report so it stays actionable.
    deduped: dict[tuple, dict] = {}
    for issue in issues:
        evidence = issue.get("evidence") or {}
        detail = evidence.get("band_hz", evidence.get("metric"))
        key = (issue["id"], tuple(issue.get("sources") or []), str(detail))
        previous = deduped.get(key)
        if previous is None or issue["severity"] > previous["severity"]:
            deduped[key] = issue
    issues = list(deduped.values())
    total_issues = len(issues)
    issues.sort(key=lambda item: -item["severity"])
    return {
        "schema_version": 1,
        "issues": issues[:max_issues],
        "total_issues": total_issues,
        "truncated": total_issues > max_issues,
        "source_levels": {
            _name(sources, source_id): {
                "rms_dbfs": _value(source, "rms_dbfs"),
                "peak_dbfs": _value(source, "peak_dbfs"),
                "crest_db": _value(source, "crest_db"),
                "integrated_lufs": _value(source, "integrated_lufs"),
            }
            for source_id, source in sources.items()
        },
        "coverage": report.get("coverage", {}),
        "decision": ("Evidence and questions only. Choose processing and "
                     "plugin parameters from the goal, references and the "
                     "plugin inventory; verify by re-measuring."),
    }
