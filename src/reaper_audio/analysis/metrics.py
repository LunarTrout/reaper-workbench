"""Level, spectrum, dynamics, stereo and transient measurements."""

from __future__ import annotations

from bisect import bisect_left

import numpy as np
from scipy.signal import stft

from ..models import AudioBuffer, MetricValue, MeasurementReport, Section
from .audio import slice_audio
from .events import detect_events
from .peaks import estimate_true_peak
from .temporal import analyze as analyze_temporal_profile
from .transients import detect_transients
from .tempo import estimate_tempo_curve

BAND_CENTERS_HZ = (
    25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0,
    250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0, 1250.0, 1600.0,
    2000.0, 2500.0, 3150.0, 4000.0, 5000.0, 6300.0, 8000.0, 10000.0,
    12500.0,
)
DB_FLOOR = -160.0


def _db(value: float, *, power: bool = False) -> float:
    if not np.isfinite(value) or value <= 0:
        return DB_FLOOR
    return float(max(DB_FLOOR, (10.0 if power else 20.0) * np.log10(value)))


def _spectrogram(audio: AudioBuffer, n_fft: int, hop: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    per_channel = []
    times = None
    frequencies = None
    for channel in range(audio.channels):
        f, t, z = stft(audio.samples[:, channel], fs=audio.sample_rate, window="hann",
                       nperseg=n_fft, noverlap=n_fft - hop,
                       boundary="zeros", padded=True)
        frequencies, times = f, t
        per_channel.append(np.abs(z) ** 2)
    power = np.mean(np.stack(per_channel, axis=0), axis=0)
    assert frequencies is not None and times is not None
    return frequencies, times, power


def _band_profile(frequencies: np.ndarray, power: np.ndarray) -> tuple[list[float], np.ndarray]:
    nyquist = float(frequencies[-1])
    ratio = 2.0 ** (1.0 / 6.0)
    levels: list[float] = []
    curves: list[np.ndarray] = []
    for center in BAND_CENTERS_HZ:
        if center >= nyquist:
            levels.append(DB_FLOOR)
            curves.append(np.full(power.shape[1], DB_FLOOR))
            continue
        low, high = center / ratio, min(center * ratio, nyquist)
        mask = (frequencies >= low) & (frequencies < high)
        if not np.any(mask):
            levels.append(DB_FLOOR)
            curves.append(np.full(power.shape[1], DB_FLOOR))
            continue
        band = np.sum(power[mask], axis=0)
        curve = np.asarray([_db(float(v), power=True) for v in band])
        curves.append(curve)
        levels.append(_db(float(np.mean(band)), power=True))
    return levels, np.stack(curves, axis=1)


def _time_levels(samples: np.ndarray, sample_rate: int, window_s: float = 0.4,
                 hop_s: float = 0.1) -> np.ndarray:
    mono_power = np.mean(samples.astype(np.float64) ** 2, axis=1)
    window = max(1, round(window_s * sample_rate))
    hop = max(1, round(hop_s * sample_rate))
    if len(mono_power) < window:
        mono_power = np.pad(mono_power, (0, window - len(mono_power)))
    csum = np.concatenate(([0.0], np.cumsum(mono_power, dtype=np.float64)))
    starts = np.arange(0, max(1, len(mono_power) - window + 1), hop)
    values = (csum[starts + window] - csum[starts]) / window
    return np.asarray([_db(float(v), power=True) for v in values])


def _unsupported_multichannel_profile(samples: np.ndarray, sample_rate: int,
                                      hop_s: float = 0.1) -> dict:
    """Keep a BPM timeline while refusing to invent a stereo downmix."""
    hop_frames = max(1, round(hop_s * sample_rate))
    starts = np.arange(0, len(samples), hop_frames, dtype=np.int64)
    dc_by_channel = np.mean(samples, axis=0)
    return {
        "hop_s": hop_frames / sample_rate,
        "times": (starts / sample_rate).tolist(),
        "fc": [],
        "cells": [],
        "mod_rate_hz": None,
        "mod_depth": None,
        "jitter_pct": None,
        "sii_proxy": None,
        "checks": {
            "dc": float(np.max(np.abs(dc_by_channel))),
            "dc_by_channel": dc_by_channel.tolist(),
            "clip_samples": int(np.count_nonzero(np.abs(samples) >= 0.999)),
        },
        "feature_coverage": "unsupported_channel_layout: provide an explicit downmix",
    }


def measure_audio(
    audio: AudioBuffer,
    *,
    section: Section | None = None,
    frame_size: int = 4096,
    hop_size: int = 1024,
    max_curve_frames: int = 20_000,
) -> MeasurementReport:
    """Measure an audio buffer. No loudness normalization or role inference is applied."""
    if frame_size < 256 or not 1 <= hop_size <= frame_size:
        raise ValueError("frame_size and hop_size are outside valid bounds")
    if max_curve_frames < 1:
        raise ValueError("max_curve_frames must be positive")
    measured = slice_audio(audio, section) if section is not None else audio
    x = measured.samples
    peak = float(np.max(np.abs(x)))
    peak_estimate = estimate_true_peak(x)
    mean_power = float(np.mean(x ** 2))
    rms = float(np.sqrt(mean_power))
    crest = _db(peak / rms) if rms > 0 else 0.0
    clip_ratio = float(np.mean(np.abs(x) >= 0.999))

    frequencies, times, power = _spectrogram(measured, frame_size, hop_size)
    band_levels, band_curves = _band_profile(frequencies, power)
    spectral_sum = np.sum(power, axis=0)
    centroid_frames = np.divide(
        np.sum(frequencies[:, None] * power, axis=0),
        spectral_sum,
        out=np.zeros_like(spectral_sum),
        where=spectral_sum > 0,
    )

    stereo_correlation: float | None = None
    side_mid_db: float | None = None
    mono_loss_db: float | None = None
    if audio.channels == 2:
        left = x[:, 0]
        right = x[:, 1]
        if np.std(left) > 0 and np.std(right) > 0:
            stereo_correlation = float(np.corrcoef(left, right)[0, 1])
        mid = (left + right) * 0.5
        side = (left - right) * 0.5
        side_mid_db = _db(float(np.sqrt(np.mean(side ** 2)) /
                                 max(np.sqrt(np.mean(mid ** 2)), 1e-15)))
        stereo_power = float((np.mean(left ** 2) + np.mean(right ** 2)) * 0.5)
        mono_power = float(np.mean(mid ** 2))
        mono_loss_db = _db(mono_power / stereo_power, power=True) if stereo_power > 0 else 0.0

    lufs: float | None = None
    lra: float | None = None
    lufs_note: str | None = None
    lra_note: str | None = None
    if measured.channels > 2:
        lufs_note = "unsupported_channel_layout: supply mono/stereo or explicit channel mapping"
        lra_note = lufs_note
    else:
        try:
            import pyloudnorm as pyln
            meter = pyln.Meter(measured.sample_rate)
            measured_lufs = float(meter.integrated_loudness(x))
            lufs = measured_lufs if np.isfinite(measured_lufs) else None
            if len(x) >= 3 * measured.sample_rate:
                measured_lra = float(meter.loudness_range(x))
                lra = measured_lra if np.isfinite(measured_lra) else None
                if lra is not None and len(x) < 60 * measured.sample_rate:
                    lra_note = "computed, but stability is not established for material under 60 s"
            else:
                lra_note = "not computed: LRA requires 3 s analysis windows"
        except Exception as exc:  # dependency/backend failures must not hide other metrics
            lufs_note = f"unavailable: {type(exc).__name__}"
            lra_note = lufs_note

    transients = detect_transients(measured)
    if measured.channels <= 2:
        temporal_profile = analyze_temporal_profile(x, fs=measured.sample_rate)
    else:
        temporal_profile = _unsupported_multichannel_profile(x, measured.sample_rate)
    event_report = detect_events(x, fs=measured.sample_rate)
    onset_times = [event["t"] for event in event_report["events"]
                   if event["kind"] == "onset"]
    tempo_curve = estimate_tempo_curve(onset_times, temporal_profile["times"])
    for cell, point in zip(temporal_profile["cells"], tempo_curve["points"]):
        cell["tempo_bpm"] = point["tempo_bpm"]
        cell["tempo_confidence"] = point["confidence"]
        cell["tempo_alternatives"] = point["alternatives"]
    for point in tempo_curve["points"]:
        point["time_s"] += measured.start_s
        point["window_s"]["start"] += measured.start_s
        point["window_s"]["end"] += measured.start_s
    tempo_curve["time_reference"] = "project/audio timeline seconds"
    for event in event_report["events"]:
        event["t"] = round(event["t"] + measured.start_s, 3)
    event_report["time_reference"] = "project/audio timeline seconds"
    temporal_profile["times"] = [time_s + measured.start_s
                                  for time_s in temporal_profile["times"]]
    event_times = [event["t"] for event in event_report["events"]]
    for cell, start_s in zip(temporal_profile["cells"], temporal_profile["times"]):
        left = bisect_left(event_times, start_s)
        right = bisect_left(event_times, start_s + temporal_profile["hop_s"])
        local_events = event_report["events"][left:right]
        cell["event_summary_2ms"] = {
            "count": len(local_events),
            "onsets": sum(event["kind"] == "onset" for event in local_events),
            "spikes": sum(event["kind"] == "spike" for event in local_events),
            "clips": sum(event["kind"] == "clip" for event in local_events),
        }
    temporal_profile["tempo"] = tempo_curve
    valid_tempos = [point["tempo_bpm"] for point in tempo_curve["points"]
                    if point["tempo_bpm"] is not None]
    short_term = _time_levels(x, measured.sample_rate)
    step = max(1, int(np.ceil(len(short_term) / max_curve_frames)))
    curve_times = np.arange(len(short_term), dtype=float) * 0.1 + measured.start_s
    metrics = {
        "peak_dbfs": MetricValue(_db(peak), "dBFS"),
        "true_peak_estimate_dbfs": MetricValue(
            peak_estimate["true_peak_dbfs_estimate"], "dBTP",
            note=(f"{peak_estimate['oversample_factor']}x polyphase FIR estimate; "
                  "not validated for standards compliance")),
        "rms_dbfs": MetricValue(_db(rms), "dBFS"),
        "crest_db": MetricValue(crest, "dB"),
        "integrated_lufs": MetricValue(lufs, "LUFS", note=lufs_note),
        "loudness_range_lu": MetricValue(lra, "LU", note=lra_note),
        "clipped_sample_ratio": MetricValue(clip_ratio, "ratio"),
        "spectral_centroid_hz": MetricValue(float(np.mean(centroid_frames)), "Hz"),
        "transient_density_per_s": MetricValue(float(transients["density_per_s"]), "events/s"),
        "transient_count": MetricValue(float(transients["event_count"]), "events"),
        "stereo_correlation": MetricValue(
            stereo_correlation, "correlation",
            note="available only for two-channel stereo input"),
        "side_mid_db": MetricValue(side_mid_db, "dB",
                                   note="available only for two-channel stereo input"),
        "mono_fold_loss_db": MetricValue(mono_loss_db, "dB",
                                          note="available only for two-channel stereo input"),
        "modulation_rate_hz": MetricValue(temporal_profile.get("mod_rate_hz"), "Hz"),
        "modulation_depth": MetricValue(temporal_profile.get("mod_depth"), "ratio"),
        "pitch_jitter_pct": MetricValue(temporal_profile.get("jitter_pct"), "%"),
        "coarse_speech_band_ratio": MetricValue(temporal_profile.get("sii_proxy"), "ratio",
                                                 note="energy ratio proxy; not standards SII"),
        "dc_offset": MetricValue(temporal_profile["checks"]["dc"], "FS"),
        "clipped_sample_count": MetricValue(float(temporal_profile["checks"]["clip_samples"]),
                                             "samples"),
        "two_ms_event_count": MetricValue(float(event_report["count"]), "events"),
        "onset_count": MetricValue(float(event_report["onsets"]), "events"),
        "spike_count": MetricValue(float(event_report["spikes"]), "events"),
        "clipping_event_count": MetricValue(float(event_report["clips"]), "events"),
        "local_tempo_median_bpm": MetricValue(
            float(np.median(valid_tempos)) if valid_tempos else None, "BPM",
            note="median of local onset-periodicity estimates; inspect per-cell confidence"),
    }
    curves = {
        "band_centers_hz": list(BAND_CENTERS_HZ),
        "band_level_dbfs": band_levels,
        "band_level_time_dbfs": band_curves[::step].tolist(),
        "rms_400ms_dbfs": short_term[::step].tolist(),
        "rms_400ms_time_s": curve_times[::step].tolist(),
        "spectrogram_time_s": (times[::step] + measured.start_s).tolist(),
        "temporal_profile": temporal_profile,
        "events_2ms": event_report,
        "peak_estimate": peak_estimate,
    }
    return MeasurementReport(
        source=measured.source,
        sample_rate=measured.sample_rate,
        channels=measured.channels,
        duration_s=measured.duration_s,
        section=section,
        metrics=metrics,
        curves=curves,
        provenance={**measured.provenance, "stage": "file_or_buffer",
                    "start_s": measured.start_s, "analysis": "reaper-audio-library",
                    "measured_section_s": {"start_s": measured.start_s,
                                            "end_s": measured.start_s + measured.duration_s}},
        coverage={"levels": "complete", "spectrum": "complete",
                  "loudness": {
                      "integrated": "available" if lufs is not None else "unavailable",
                      "range": "available" if lra is not None else "unavailable",
                      "integrated_note": lufs_note,
                      "range_note": lra_note,
                  },
                  "true_peak": peak_estimate["coverage"],
                  "stereo": ("complete" if measured.channels == 2 else
                             "not_applicable" if measured.channels == 1 else
                             "unsupported_channel_layout"),
                  "transients": transients.get("coverage", "unknown"),
                  "temporal_profile": temporal_profile.get(
                      "feature_coverage", "complete"),
                  "short_events": "complete",
                  "local_tempo": ("estimated" if valid_tempos else
                                  "insufficient_signal: no confident local tempo estimate")},
    )
