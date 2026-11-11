"""Audio loading and section selection without implicit normalization."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from ..errors import ValidationError
from ..models import AudioBuffer, Section


def load_audio(path: str | Path, section: Section | None = None) -> AudioBuffer:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValidationError("Audio file does not exist", details={"path": str(path)})
    try:
        info = sf.info(path)
        if info.frames <= 0 or info.samplerate <= 0:
            raise ValidationError("Audio file contains no samples", details={"path": str(path)})
        duration_s = info.frames / info.samplerate
        if section is not None and section.end_s > duration_s + 0.5 / info.samplerate:
            raise ValidationError("Requested section extends beyond the audio file",
                                  details={"path": str(path), "duration_s": duration_s,
                                           "section": {"start_s": section.start_s,
                                                       "end_s": section.end_s}})
        start = 0 if section is None else max(0, round(section.start_s * info.samplerate))
        stop = info.frames if section is None else min(info.frames,
                                                        round(section.end_s * info.samplerate))
        if stop <= start:
            raise ValidationError("Requested section is outside the audio file",
                                  details={"path": str(path), "section": section})
        samples, sample_rate = sf.read(path, start=start, stop=stop,
                                       dtype="float64", always_2d=True)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("Unable to decode audio", details={"path": str(path),
                                                                  "reason": str(exc)}) from exc
    actual_start = start / sample_rate
    return AudioBuffer(samples=samples, sample_rate=int(sample_rate), source=str(path),
                       start_s=actual_start,
                       provenance={"decoder": "libsndfile",
                                   "file_frames": int(info.frames),
                                   "file_channels": int(info.channels),
                                   "section_requested": None if section is None else
                                       {"start_s": section.start_s, "end_s": section.end_s}})


def slice_audio(audio: AudioBuffer, section: Section) -> AudioBuffer:
    buffer_end_s = audio.start_s + audio.duration_s
    half_sample_s = 0.5 / audio.sample_rate
    if (section.start_s < audio.start_s - half_sample_s or
            section.end_s > buffer_end_s + half_sample_s):
        raise ValidationError("Requested section is not fully covered by the audio buffer",
                              details={"section": {"start_s": section.start_s,
                                                   "end_s": section.end_s},
                                       "buffer": {"start_s": audio.start_s,
                                                  "end_s": buffer_end_s}})
    start = max(0, round((section.start_s - audio.start_s) * audio.sample_rate))
    stop = min(audio.frames, round((section.end_s - audio.start_s) * audio.sample_rate))
    if stop <= start:
        raise ValidationError("Requested section contains no audio samples",
                              details={"section": {"start_s": section.start_s,
                                                   "end_s": section.end_s},
                                       "buffer": {"start_s": audio.start_s,
                                                  "end_s": buffer_end_s}})
    return AudioBuffer(samples=audio.samples[start:stop], sample_rate=audio.sample_rate,
                       source=audio.source,
                       start_s=audio.start_s + start / audio.sample_rate,
                       provenance={**audio.provenance,
                                   "section_requested": {"start_s": section.start_s,
                                                         "end_s": section.end_s},
                                   "section_actual": {
                                       "start_s": audio.start_s + start / audio.sample_rate,
                                       "end_s": audio.start_s + stop / audio.sample_rate}})


def write_audio(audio: AudioBuffer, path: str | Path, *, subtype: str = "FLOAT") -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    sf.write(target, audio.samples, audio.sample_rate, subtype=subtype)
    return target
