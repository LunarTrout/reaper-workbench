"""Compact, human-readable state of one plug-in instance.

A full parameter dump is exact but unusable as context: FabFilter Pro-Q 4 alone
exposes 603 parameters (24 bands x 25), so one track costs hundreds of kilobytes
for a handful of facts a mix decision actually needs.

This module derives a short summary from the parameter names a plug-in already
publishes.  It never invents values: every field is copied from the plug-in's
own ``formatted`` text, and anything it cannot map is reported as ``unmapped``
instead of being silently dropped.

Summaries are cached by :mod:`reaper_audio.context.plugin_state_store`; the cache
key includes a fingerprint of the parameter vector, so a summary can never
outlive the state it describes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

PLUGIN_SUMMARY_SCHEMA = "reaper-workbench.plugin-summary/1"

# Field sets are keyed by the parameter names each plug-in family actually uses.
# Adding a family is one entry here, not a new code path.
DYNAMICS_FIELDS = ("Threshold", "Ratio", "Knee", "Range", "Attack", "Release",
                   "Auto Release", "Mix", "Wet", "Style")
LIMITER_FIELDS = ("Gain", "Output Level", "Ceiling", "Attack", "Release",
                  "Style", "True Peak Limiting", "Channel Link Release")
REVERB_FIELDS = ("Mix", "PreDelay", "Decay", "Size", "Attack", "BassMult",
                 "BassXover", "HighShelf", "HighFreq", "EarlyDiffusion",
                 "LateDiffusion", "ModRate", "ModDepth", "HighCut", "LowCut",
                 "ColorMode", "ReverbMode")
DELAY_FIELDS = ("Length", "Feedback", "Wet", "Dry", "Lowpass", "Highpass",
                "Taps", "Width")
SATURATION_FIELDS = ("Drive", "Input", "Output", "Mix", "Tone", "Bias", "Dynamics",
                     "Feedback", "Amount")
AMP_FIELDS = ("Gain", "Bass", "Middle", "Mid", "Treble", "Presence", "Depth",
              "Master", "Output", "Channel", "Cabinet", "Mic")

_CLASS_RULES: Sequence[tuple[re.Pattern[str], str]] = (
    (re.compile(r"pro-q", re.I), "eq"),
    (re.compile(r"pro-c", re.I), "compressor"),
    (re.compile(r"pro-l|limiter", re.I), "limiter"),
    (re.compile(r"pro-ds|de-?esser", re.I), "deesser"),
    (re.compile(r"valhalla|vintageverb|reverb", re.I), "reverb"),
    (re.compile(r"readelay|delay", re.I), "delay"),
    (re.compile(r"3-band eq|reaeq", re.I), "eq"),
    (re.compile(r"saturn|decapitator|saturat", re.I), "saturation"),
    (re.compile(r"fortin|nameless|neural dsp|archetype|amp sim| amplitube", re.I), "amp"),
    (re.compile(r"kontakt|superior drummer|ezbass|ample|serum|vital|"
                r"sylenth|massive|absynth|omnisphere", re.I), "instrument"),
)

_BAND_USED = re.compile(r"^Band (\d+) Used$")
_BAND_FIELD = re.compile(r"^Band (\d+) (Frequency|Gain|Q|Shape|Slope)$")


@dataclass(frozen=True)
class PluginDescriptor:
    """What the summary needs to know about one plug-in instance."""

    name: str
    active: bool
    parameters: Sequence[Mapping[str, Any]]


def classify(name: str) -> str:
    for pattern, family in _CLASS_RULES:
        if pattern.search(name):
            return family
    return "unmapped"


def parameter_fingerprint(parameters: Iterable[Mapping[str, Any]]) -> str:
    """Hash the exact parameter vector the summary was derived from."""
    digest = hashlib.sha256()
    for parameter in sorted(parameters, key=lambda row: int(row.get("index", 0))):
        digest.update(str(parameter.get("index")).encode("utf-8"))
        digest.update(b"=")
        value = parameter.get("normalized")
        digest.update(repr(float(value) if isinstance(value, (int, float)) else value)
                      .encode("utf-8"))
        digest.update(b";")
    return "sha256:" + digest.hexdigest()


def _values(parameters: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {str(row.get("name")): str(row.get("formatted"))
            for row in parameters if row.get("name") is not None}


def _pick(values: Mapping[str, str], names: Iterable[str]) -> dict[str, str]:
    picked: dict[str, str] = {}
    for name in names:
        if name in values and values[name] not in ("", "Unused"):
            picked[name] = values[name]
    return picked


def _bands(parameters: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    used: set[int] = set()
    fields: dict[int, dict[str, str]] = {}
    for row in parameters:
        name = str(row.get("name") or "")
        used_match = _BAND_USED.match(name)
        if used_match and str(row.get("formatted")) == "Used":
            used.add(int(used_match.group(1)))
            continue
        field_match = _BAND_FIELD.match(name)
        if field_match:
            band = int(field_match.group(1))
            fields.setdefault(band, {})[field_match.group(2)] = str(row.get("formatted"))
    result: list[dict[str, str]] = []
    for band in sorted(used):
        row = {"band": str(band)}
        row.update(fields.get(band, {}))
        result.append(row)
    return result


def _preset(values: Mapping[str, str]) -> str | None:
    for name, value in values.items():
        lowered = name.lower()
        if any(token in lowered for token in ("preset", "program", "patch",
                                              "library", "kit", "instrument")):
            if value not in ("", "Default", "Unused"):
                return value
    return None


def summarize(plugin: PluginDescriptor) -> dict[str, Any]:
    """Return a compact state for one plug-in, or an explicit ``unmapped``."""
    family = classify(plugin.name)
    values = _values(plugin.parameters)
    summary: dict[str, Any] = {
        "schema": PLUGIN_SUMMARY_SCHEMA,
        "name": plugin.name,
        "family": family,
        "active": bool(plugin.active),
        "parameter_count": len(plugin.parameters),
        "fingerprint": parameter_fingerprint(plugin.parameters),
    }
    if family == "eq":
        bands = _bands(plugin.parameters)
        summary["bands"] = bands
        summary["used_bands"] = len(bands)
        summary["mapped"] = bool(bands) or any(
            name.endswith(" Used") for name in values)
    elif family == "compressor":
        summary["fields"] = _pick(values, DYNAMICS_FIELDS)
        summary["mapped"] = bool(summary["fields"])
    elif family == "limiter":
        summary["fields"] = _pick(values, LIMITER_FIELDS)
        summary["mapped"] = bool(summary["fields"])
    elif family == "deesser":
        summary["fields"] = _pick(values, DYNAMICS_FIELDS)
        summary["mapped"] = bool(summary["fields"])
    elif family == "reverb":
        summary["fields"] = _pick(values, REVERB_FIELDS)
        summary["mapped"] = bool(summary["fields"])
    elif family == "saturation":
        matched = {name: value for name, value in values.items()
                   if any(token.lower() in name.lower() for token in SATURATION_FIELDS)}
        summary["fields"] = matched
        summary["mapped"] = bool(matched)
    elif family == "amp":
        matched = {name: value for name, value in values.items()
                   if any(token.lower() in name.lower() for token in AMP_FIELDS)}
        summary["fields"] = matched
        summary["mapped"] = bool(matched)
    elif family == "delay":
        matched = {name: value for name, value in values.items()
                   if any(token.lower() in name.lower() for token in DELAY_FIELDS)}
        summary["fields"] = matched
        summary["mapped"] = bool(matched)
    elif family == "instrument":
        preset = _preset(values)
        summary["preset"] = preset
        summary["mapped"] = preset is not None
    else:
        summary["mapped"] = False
    if not summary.get("mapped"):
        summary["reason"] = "no_parameter_rule_for_this_plugin"
    return summary
