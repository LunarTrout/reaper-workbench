"""Run the available analysis and requested comparisons in one Python call."""

from __future__ import annotations

from collections.abc import Mapping
from itertools import combinations
from math import isfinite
from typing import Literal

import numpy as np

from ..errors import ValidationError
from ..models import AudioBuffer, MeasurementReport, Section
from .compare import compare_reports, reference_corridor
from .metrics import measure_audio
from .partial import attribute_partial_loudness
from .phase import compare_track_phase
from .temporal import masking as analyze_masking


def _band_balance_timeline(source_reports: Mapping[str, MeasurementReport]) -> dict:
    """Compare per-band track levels at matching temporal-profile timestamps.

    This reports measured level rankings only. It does not label a louder source
    as psychoacoustically masking another source.
    """
    observations: dict[float, dict[str, list[float]]] = {}
    band_centers: list[float] = []
    source_ticks: dict[str, int] = {}

    for source_id, report in source_reports.items():
        curves = report.curves
        profile = curves.get("temporal_profile", {})
        centers = profile.get("fc", [])
        if not band_centers and centers:
            band_centers = [float(value) for value in centers]
        times = profile.get("times", [])
        cells = profile.get("cells", [])
        source_ticks[source_id] = 0
        for time_s, cell in zip(times, cells):
            bands = cell.get("bands")
            if not isinstance(bands, list) or not bands:
                continue
            try:
                levels = [float(value) for value in bands]
            except (TypeError, ValueError):
                continue
            if not np.isfinite(levels).all():
                continue
            # Temporal-profile timestamps currently have millisecond precision.
            time_key = round(float(time_s), 3)
            observations.setdefault(time_key, {})[source_id] = levels
            source_ticks[source_id] += 1

    output_cells = []
    comparable_ticks = 0
    all_source_ticks = 0
    for time_s in sorted(observations):
        source_levels = observations[time_s]
        source_ids = sorted(source_levels)
        all_source_ticks += int(len(source_ids) == len(source_reports))
        common_band_count = min(
            [len(band_centers), *(len(source_levels[key]) for key in source_ids)]
        ) if band_centers and source_ids else 0
        band_rows = []
        if len(source_ids) >= 2:
            comparable_ticks += 1
            for band_index in range(common_band_count):
                ranked = sorted(
                    ((source_id, source_levels[source_id][band_index])
                     for source_id in source_ids),
                    key=lambda item: (-item[1], item[0]),
                )
                band_rows.append({
                    "center_hz": band_centers[band_index],
                    "highest": {"source_id": ranked[0][0], "level_dbfs": ranked[0][1]},
                    "runner_up": {"source_id": ranked[1][0], "level_dbfs": ranked[1][1]},
                    "margin_db": ranked[0][1] - ranked[1][1],
                })
        output_cells.append({
            "time_s": time_s,
            "available_sources": source_ids,
            "bands": band_rows,
        })

    return {
        "cells": output_cells,
        "coverage": {
            "source_count": len(source_reports),
            "source_ticks": source_ticks,
            "timeline_ticks": len(output_cells),
            "comparable_ticks": comparable_ticks,
            "ticks_with_all_sources": all_source_ticks,
            "alignment": "cell start timestamps must match at 1 ms precision; no interpolation",
            "status": "available" if comparable_ticks else "insufficient_overlap",
        },
        "interpretation": (
            "Per-band level ranking at aligned times; level dominance is evidence, "
            "not a psychoacoustic masking judgment."
        ),
    }


def _render_provenance(buffer: AudioBuffer | None) -> dict:
    """Declared render facts a buffer may carry for the attribution gate.

    ``partial.attribute_partial_loudness`` needs to know which node, stage and
    channel view a render came from. Buffers that do not carry those keys stay
    undeclared, and the gate then reports missing provenance instead of
    attributing anything.
    """
    provenance = buffer.provenance if isinstance(buffer, AudioBuffer) else {}
    return {key: provenance[key] for key in ("node", "stage", "channel_view", "window")
            if provenance.get(key) is not None}


def analyze_bundle(
    sources: Mapping[str, AudioBuffer],
    *,
    references: Mapping[str, AudioBuffer] | None = None,
    reference_sections: Mapping[str, Section] | None = None,
    section: Section | None = None,
    anchor_key: str | None = None,
    mix_key: str | None = None,
    partial_model: str = "iso532-2",
    partial_hop_s: float = 0.2,
    include_pairwise: bool = False,
    phase_pairs: list[tuple[str, str]] | Literal["all"] | None = None,
    phase_max_lag_ms: float = 10.0,
    precomputed_reports: Mapping[str, object] | None = None,
) -> dict:
    """Analyze every source and compare it with supplied references in one call.

    The bundle runs every currently available measurement for each supplied
    buffer. It does not assign a mix-quality score or invent goals. References
    are optional inputs; absent references produce an explicit coverage status.
    Pairwise comparisons are opt-in because their count grows quadratically
    with the number of tracks. Phase checks accept selected ID pairs or "all";
    they never modify or auto-align the source audio.
    """
    if not isinstance(sources, Mapping) or not sources:
        raise ValidationError("sources must be a non-empty mapping of IDs to AudioBuffer")
    if any(not isinstance(key, str) or not key.strip() for key in sources):
        raise ValidationError("Every source needs a non-empty string ID")
    if precomputed_reports is None and any(
            not isinstance(audio, AudioBuffer) for audio in sources.values()):
        raise ValidationError("Every source value must be an AudioBuffer")
    if references is None:
        references = {}
    if not isinstance(references, Mapping):
        raise ValidationError("references must be a mapping of IDs to AudioBuffer")
    if any(not isinstance(key, str) or not key.strip() for key in references):
        raise ValidationError("Every reference needs a non-empty string ID")
    if any(not isinstance(audio, AudioBuffer) for audio in references.values()):
        raise ValidationError("Every reference value must be an AudioBuffer")
    if reference_sections is None:
        reference_sections = {}
    if not isinstance(reference_sections, Mapping):
        raise ValidationError("reference_sections must map reference IDs to Section values")
    if any(not isinstance(key, str) or not key.strip()
           for key in reference_sections):
        raise ValidationError("Reference section IDs must be non-empty strings")
    unknown_reference_ids = set(reference_sections) - set(references)
    if unknown_reference_ids:
        raise ValidationError("Every reference section must identify a supplied reference",
                              details={"unknown_reference_ids":
                                       sorted(unknown_reference_ids)})
    if any(not isinstance(value, Section) for value in reference_sections.values()):
        raise ValidationError("Every reference section must be a Section")
    if set(sources) & set(references):
        raise ValidationError("Source and reference IDs must be distinct")
    if anchor_key is not None and anchor_key not in sources:
        raise ValidationError("anchor_key must identify one of the supplied sources",
                              details={"anchor_key": anchor_key,
                                       "source_ids": sorted(sources)})
    if mix_key is not None and mix_key not in sources:
        raise ValidationError("mix_key must identify one of the supplied sources",
                              details={"mix_key": mix_key,
                                       "source_ids": sorted(sources)})
    if mix_key is not None and len(sources) < 2:
        raise ValidationError("partial loudness needs a mix and at least one source stem")
    if not isinstance(include_pairwise, bool):
        raise ValidationError("include_pairwise must be a boolean")
    if (isinstance(phase_max_lag_ms, (bool, np.bool_))
            or not isinstance(phase_max_lag_ms, (int, float, np.integer, np.floating))
            or not isfinite(float(phase_max_lag_ms)) or phase_max_lag_ms < 0):
        raise ValidationError("phase_max_lag_ms must be finite and non-negative")
    phase_max_lag_ms = float(phase_max_lag_ms)

    if isinstance(phase_pairs, str) and phase_pairs == "all":
        selected_phase_pairs = list(combinations(sources, 2))
    elif phase_pairs is None:
        selected_phase_pairs = []
    elif isinstance(phase_pairs, (list, tuple)):
        selected_phase_pairs = []
        seen_phase_pairs: set[frozenset[str]] = set()
        for pair in phase_pairs:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(not isinstance(key, str) for key in pair)):
                raise ValidationError("Each phase pair must contain two source IDs")
            left_id, right_id = pair
            if left_id not in sources or right_id not in sources:
                raise ValidationError("Phase pairs must refer to supplied sources",
                                      details={"pair": list(pair),
                                               "source_ids": sorted(sources)})
            if left_id == right_id:
                raise ValidationError("A source cannot be compared with itself")
            normalized = frozenset(pair)
            if normalized in seen_phase_pairs:
                continue
            selected_phase_pairs.append((left_id, right_id))
            seen_phase_pairs.add(normalized)
    else:
        raise ValidationError("phase_pairs must be None, 'all', or source ID pairs")

    if precomputed_reports is None:
        source_reports = {key: measure_audio(audio, section=section)
                          for key, audio in sources.items()}
    else:
        if set(precomputed_reports) != set(sources):
            raise ValidationError("precomputed reports must match the supplied source IDs")
        source_reports = dict(precomputed_reports)
    # A project section and a reference-file section live on different
    # timelines. References default to their full audio; compare a selected
    # musical region only when its reference-local window is supplied.
    reference_reports = {
        key: measure_audio(audio, section=reference_sections.get(key))
        for key, audio in references.items()
    }

    ref_comparisons = {
        source_id: {
            reference_id: compare_reports(source_report, reference_reports[reference_id])
            for reference_id in reference_reports
        }
        for source_id, source_report in source_reports.items()
    }
    corridors = {
        source_id: reference_corridor(source_report, list(reference_reports.values()))
        for source_id, source_report in source_reports.items()
    } if reference_reports else {}

    anchor_comparisons = {}
    if anchor_key is not None:
        anchor_report = source_reports[anchor_key]
        anchor_comparisons = {
            source_id: compare_reports(anchor_report, report)
            for source_id, report in source_reports.items()
            if source_id != anchor_key
        }

    pairwise = {}
    if include_pairwise:
        source_ids = list(source_reports)
        for left_index, left_id in enumerate(source_ids):
            for right_id in source_ids[left_index + 1:]:
                pairwise[f"{left_id}::{right_id}"] = compare_reports(
                    source_reports[left_id], source_reports[right_id]
                )

    phase_comparisons = []
    for left_id, right_id in selected_phase_pairs:
        phase_comparisons.append({
            "source_a_id": left_id,
            "source_b_id": right_id,
            **compare_track_phase(
                sources[left_id], sources[right_id], section=section,
                max_lag_ms=phase_max_lag_ms,
            ),
        })
    phase_statuses = [item["coverage"]["status"] for item in phase_comparisons]
    if phase_pairs is None:
        phase_coverage = "not_requested: pass phase_pairs=[('track_a', 'track_b')] or 'all'"
    elif not selected_phase_pairs:
        phase_coverage = "not_applicable: no distinct source pairs requested"
    elif all(status == "available" for status in phase_statuses):
        phase_coverage = "available"
    elif any(status == "available" for status in phase_statuses):
        phase_coverage = "partial: one or more requested pairs have insufficient coverage"
    else:
        phase_coverage = "unavailable: no requested pair has sufficient coverage"

    masking_inputs = {
        source_id: {
            "name": source_id,
            "cells": report.curves["temporal_profile"]["cells"],
        }
        for source_id, report in source_reports.items()
    }
    masking = analyze_masking(masking_inputs)
    masking["interpretation"] = (
        "Relative band-level dominance heuristic; not a psychoacoustic masking model."
    )
    band_balance_timeline = _band_balance_timeline(source_reports)

    partial_loudness = None
    partial_coverage = "not_requested: pass mix_key to identify the rendered mix source"
    if mix_key is not None:
        partial_sources = {key: audio for key, audio in sources.items() if key != mix_key}
        mix_render = _render_provenance(sources[mix_key])
        partial_loudness = attribute_partial_loudness(
            sources[mix_key], partial_sources, section=section,
            model=partial_model, hop_s=partial_hop_s,
            node=mix_render.get("node"), stage=mix_render.get("stage"),
            channel_view=mix_render.get("channel_view"),
            source_provenance={key: _render_provenance(audio)
                               for key, audio in partial_sources.items()},
        )
        if partial_loudness["status"] != "available":
            partial_coverage = f"unavailable: {partial_loudness['reason']}"
        else:
            partial_coverage = "available"
        for source_id, source_data in (partial_loudness.get("sources") or {}).items():
            profile = source_reports[source_id].curves["temporal_profile"]
            series = source_data["time_series"]
            source_times = np.asarray(series["time_s"], dtype=np.float64)
            for cell, time_s in zip(profile["cells"], profile["times"]):
                cell["partial_loudness"] = {
                    "model": partial_loudness["model"],
                    "status": partial_loudness["status"],
                    "node": partial_loudness["node"],
                    "stage": partial_loudness["stage"],
                    "mix_sones": float(np.interp(time_s, source_times,
                                                 series["mix_sones"])),
                    "source_solo_sones": float(np.interp(
                        time_s, source_times, series["source_solo_sones"]
                    )),
                    "source_partial_sones": float(np.interp(
                        time_s, source_times, series["source_partial_sones"]
                    )),
                    "partial_to_solo_ratio": float(np.interp(
                        time_s, source_times, series["partial_to_solo_ratio"]
                    )),
                    "regions": {
                        name: {
                            "source_solo_sones": float(np.interp(
                                time_s, source_times, region_series["source_solo_sones"]
                            )),
                            "source_partial_sones": float(np.interp(
                                time_s, source_times, region_series["source_partial_sones"]
                            )),
                            "partial_to_solo_ratio": float(np.interp(
                                time_s, source_times, region_series["partial_to_solo_ratio"]
                            )),
                        }
                        for name, region in source_data["regions"].items()
                        for region_series in [region["time_series"]]
                    },
                    "summary": {
                        "share_of_partial_loudness_pct":
                            source_data["share_of_partial_loudness_pct"],
                        "sensitivity_sones_per_db":
                            source_data["sensitivity_sones_per_db"],
                    },
                }

    return {
        "schema_version": 1,
        "section": None if section is None else {
            "start_s": section.start_s, "end_s": section.end_s,
        },
        "sources": {key: report.to_dict() for key, report in source_reports.items()},
        "references": {key: report.to_dict() for key, report in reference_reports.items()},
        "reference_comparisons": ref_comparisons,
        "reference_corridors": corridors,
        "anchor_key": anchor_key,
        "anchor_comparisons": anchor_comparisons,
        "pairwise_comparisons": pairwise,
        "pairwise_phase": phase_comparisons,
        "masking_evidence": masking,
        "cross_source_band_timeline": band_balance_timeline,
        "partial_loudness": partial_loudness,
        "coverage": {
            "sources_analyzed": len(source_reports),
            "references_analyzed": len(reference_reports),
            "reference_comparison": "available" if reference_reports else
                "not_requested: supply references to compare against them",
            "pairwise_comparisons": "complete" if include_pairwise else
                "not_requested: enable include_pairwise for all source pairs",
            "pairwise_phase": phase_coverage,
            "partial_loudness": partial_coverage,
            "cross_source_band_timeline":
                band_balance_timeline["coverage"]["status"],
            "interpretation": (
                "Measurements and numeric comparisons only; no mix recipe or "
                "quality score is inferred."
            ),
        },
    }
