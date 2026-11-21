"""Rhythm hypotheses from measured attack and sustain evidence."""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil, gcd, isfinite

import numpy as np

from ..errors import ValidationError

DEFAULT_DIVISIONS = (1, 2, 3, 4, 6, 8)
MAX_SEGMENTS = 512


@dataclass(frozen=True)
class Attack:
    id: str
    time: float
    qn: float
    confidence: float
    novelty: float
    prominence: float
    energy_jump: float


def _grid_division(slot: int, division: int) -> int:
    return division // gcd(slot, division)


def _fit(attacks, slots, tolerance):
    """Find an ordered one-to-one assignment with empty slots and rejected peaks."""
    m, n = len(attacks), len(slots)
    costs = np.full((m+1, n+1), np.inf)
    moves = np.zeros((m+1, n+1), dtype=np.int8)
    costs[0, :] = 0.
    for i, attack in enumerate(attacks, 1):
        skip = .28 + 1.45 * attack.confidence
        costs[i, 0] = costs[i-1, 0] + skip
        moves[i, 0] = 1
        for j, (time, _, division) in enumerate(slots, 1):
            leave = costs[i-1, j] + skip
            empty = costs[i, j-1]
            cost, move = (leave, 1) if leave < empty else (empty, 2)
            error = abs(attack.time-time)
            if error <= tolerance and attack.confidence >= .12:
                # Prefer musically simpler positions when they explain the
                # observed timing just as well.
                fit = (costs[i-1, j-1] + .95*(error/tolerance)**2
                       + .28*(1-attack.confidence) + .095*np.log2(division))
                if fit < cost:
                    cost, move = fit, 3
            costs[i, j], moves[i, j] = cost, move
    matches = []
    i, j = m, n
    while i:
        move = moves[i, j]
        if move == 1:
            i -= 1
        elif move == 2:
            j -= 1
        else:
            matches.append((attacks[i-1], slots[j-1]))
            i, j = i-1, j-1
    return list(reversed(matches)), float(costs[m, n])


def _segments(tempo, first_qn, last_qn):
    """Use the meter beat as the briefest location where the rhythm may change."""
    bar, _ = tempo.qn_to_bar_beat(first_qn)
    if bar < 1:
        bar = 1
    segments = []
    while tempo.bar_start_qn(bar) < last_qn:
        start = tempo.bar_start_qn(bar)
        stop = tempo.bar_start_qn(bar+1)
        numerator, denominator, beat_length, _ = tempo.meter_at_bar(bar)
        # Compound meter groups written eighths into dotted-quarter pulses.
        if denominator >= 8 and numerator >= 6 and numerator % 3 == 0:
            beat_length *= 3
        beat = 1
        while start < stop-1e-8:
            end = min(stop, start+beat_length)
            if end > first_qn:
                segments.append((bar, beat, start, end))
                if len(segments) > MAX_SEGMENTS:
                    raise ValidationError("Analyze a shorter item window for rhythm mapping",
                                          details={"max_segments": MAX_SEGMENTS})
            beat += 1
            start = end
        bar += 1
    return segments


def _periodic_pitch(samples, rate):
    """Return a monophonic pitch cue only when the window is clearly periodic."""
    if len(samples) < .035*rate:
        return None
    channel = samples[:, int(np.argmax(np.mean(samples**2, axis=0)))]
    channel = channel-np.mean(channel)
    if np.max(np.abs(channel)) < 1e-6:
        return None
    low = max(1, int(rate/800))
    high = min(int(rate/65), len(channel)//3)
    if high <= low:
        return None
    fft_size = 1 << (2*len(channel)-1).bit_length()
    spectrum = np.fft.rfft(channel, n=fft_size)
    correlation = np.fft.irfft(abs(spectrum)**2, n=fft_size)[:high+1]
    partial_energy = np.r_[0, np.cumsum(channel**2)]
    lags = np.arange(low, high+1)
    scale = np.sqrt(partial_energy[len(channel)-lags] *
                    (partial_energy[-1]-partial_energy[lags]))
    values = correlation[lags]/np.maximum(scale, 1e-12)
    peaks = np.flatnonzero((values[1:-1] >= values[:-2]) &
                           (values[1:-1] >= values[2:])) + 1
    if not len(peaks):
        return None
    best = max(float(np.max(values[peaks]))-.08, .74)
    candidates = peaks[values[peaks] >= best]
    if not len(candidates):
        return None
    index = candidates[0]
    return {"hz": float(rate/lags[index]), "periodicity": float(values[index])}


def _sound_evidence(audio, attacks, source_origin, window_end, profile):
    """Measure envelope support and broad spectral motion between attack candidates."""
    samples = np.asarray(audio.samples, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    rate = audio.sample_rate
    frame = max(16, round(rate*.008))
    n = len(samples)//frame
    if not n:
        return {}
    rms = np.sqrt(np.mean(samples[:n*frame].reshape(n, frame, -1)**2, axis=(1, 2)))
    ends = [attack.time for attack in attacks[1:]] + [window_end]
    evidence = {}
    for attack, limit in zip(attacks, ends):
        start = max(0, round((attack.time-source_origin)*rate/frame))
        stop = min(n, round((limit-source_origin)*rate/frame))
        values = rms[start:stop]
        gap = limit-attack.time
        if len(values) < 5:
            evidence[attack.id] = {"next_candidate_gap_s": gap, "sound_end_s": None,
                                   "state": "insufficient_between_fronts"}
            continue
        level = float(np.max(values[:min(len(values), 8)]))
        nearby = rms[max(0, start-12):min(n, stop+12)]
        floor = float(np.percentile(nearby, 10))
        # Require a sustained low-energy run. An envelope end is evidence of
        # release, and is not treated as the notated duration.
        low = values <= max(1e-7, level*.12, min(floor*1.5, level*.20))
        hold = 4 if profile in ("vocal", "sustained") else 3
        quiet = np.flatnonzero(np.convolve(low.astype(int), np.ones(hold, dtype=int),
                                          mode="valid") == hold)
        quiet = quiet[quiet >= 2]
        release = (source_origin+(start+int(quiet[0]))*frame/rate
                   if quiet.size else None)
        early = samples[start*frame:min(stop, start+4)*frame]
        late = samples[max(start, stop-4)*frame:stop*frame]
        # Channel-wise spectra retain energy in opposite-polarity channels.
        def spectral_shape(part):
            spec = np.linalg.norm(np.fft.rfft(part, n=4*frame, axis=0), axis=1)
            return spec/(np.linalg.norm(spec)+1e-15)
        similarity = (float(spectral_shape(early) @ spectral_shape(late))
                      if np.max(np.abs(early)) > 1e-7 and np.max(np.abs(late)) > 1e-7
                      else None)
        evidence[attack.id] = {"next_candidate_gap_s": gap, "sound_end_s": release,
                               "measured_sound_duration_s": (max(0., release-attack.time)
                                                             if release is not None else None),
                               "state": ("energy_drop_before_next_front" if release is not None
                                         else "no_resolved_release_before_next_front"),
                               "end_resolution_s": frame/rate,
                               "early_late_spectral_similarity": similarity}
        if profile in ("vocal", "sustained"):
            # Pitch is optional evidence. A noisy consonant or polyphonic
            # segment can leave either window without a defensible reading.
            sound_stop = (min(stop, round((release-source_origin)*rate/frame))
                          if release is not None else stop)
            early_pitch = samples[start*frame:min(sound_stop, start+7)*frame]
            late_pitch = samples[max(start, sound_stop-7)*frame:sound_stop*frame]
            evidence[attack.id]["pitch_observations"] = {
                "early": _periodic_pitch(early_pitch, rate),
                "late": _periodic_pitch(late_pitch, rate),
            }
    return evidence


def infer_rhythm(audio, events, tempo, *, start_s, end_s, source_origin,
                 subdivisions=DEFAULT_DIVISIONS, phase_s=0., tolerance_s=.046,
                 profile="auto", context_events=()):
    """Give distinct, ranked rhythm interpretations and observations for each beat."""
    divisions = tuple(sorted(set(subdivisions)))
    if (not divisions or len(divisions) > 12 or
            any(type(d) is not int or not 1 <= d <= 32 for d in divisions)):
        raise ValidationError("Pass 1 to 12 rhythmic divisions between 1 and 32")
    if not isfinite(phase_s) or not 0 < tolerance_s <= .25 or end_s <= start_s:
        raise ValidationError("Invalid rhythm interval, phase or tolerance")
    if len(events) > 4096:
        raise ValidationError("Analyze a shorter item window for rhythm mapping",
                              details={"max_events": 4096})
    attacks = sorted((Attack(
        e["id"], float(e["project_s"]), tempo.time_to_qn(float(e["project_s"])-phase_s),
        float(e["confidence"]), float(e.get("novelty", 0)),
        float(e.get("prominence", 0)), float(e.get("energy_jump", 0)))
        for e in events if start_s <= e["project_s"] < end_s), key=lambda a: a.time)
    observations = _sound_evidence(audio, attacks, source_origin, end_s, profile)
    qn_start = tempo.time_to_qn(start_s-phase_s)
    qn_end = tempo.time_to_qn(end_s-phase_s)
    segments = []
    # A small anticipation window lets a played downbeat arrive slightly early.
    # Half the densest cell avoids borrowing earlier, genuine subdivisions.
    lead_qn = min(.06, .46/max(divisions))
    qns = np.array([a.qn for a in attacks])
    context = np.sort([float(e["project_s"]) for e in context_events
                       if float(e["confidence"]) >= .35])
    for bar, beat, begin, end in _segments(tempo, qn_start, qn_end):
        lo, hi = np.searchsorted(qns, [begin-lead_qn, end-lead_qn])
        local = attacks[lo:hi]
        if not segments and lo:
            local = attacks[:hi]
        if end >= qn_end and hi < len(attacks):
            local = attacks[lo if segments else 0:]
        start = (max(start_s, tempo.qn_to_time(begin-lead_qn)+phase_s)
                 if segments else start_s)
        finish = min(end_s, tempo.qn_to_time(end-lead_qn)+phase_s)
        if end >= qn_end:
            finish = end_s
        if finish <= start:
            continue
        unique = {}
        for subdivision in divisions:
            beat_width = end-begin
            slots = [(tempo.qn_to_time(begin+k*beat_width/subdivision)+phase_s,
                      begin+k*beat_width/subdivision, _grid_division(k, subdivision))
                     for k in range(subdivision)]
            slots = [slot for slot in slots if start_s-1e-8 <= slot[0] < end_s]
            if not slots or not local:
                continue
            matched, cost = _fit(local, slots, tolerance_s)
            if not matched:
                continue
            chosen = []
            for a, (time, qn, _) in matched:
                cue_index = int(np.searchsorted(context, time))
                cue_distances = [abs(context[i]-time)
                                 for i in (cue_index-1, cue_index) if 0 <= i < len(context)]
                cue = float(min(cue_distances)) if cue_distances else None
                chosen.append({"event_id": a.id, "observed_s": a.time,
                               "target_s": time, "target_qn": qn,
                               "shift_s": time-a.time, "confidence": a.confidence,
                               "nearby_context_attack_distance_s": cue})
            cost += .13*np.log2(subdivision)
            cost -= .14*sum(x["nearby_context_attack_distance_s"] is not None and
                            x["nearby_context_attack_distance_s"] < .022 for x in chosen)
            ids = {a.id for a, _ in matched}
            key = tuple((a.id, round(qn, 7)) for a, (_, qn, _) in matched)
            hypothesis = {"division_per_beat": subdivision, "cost": float(cost),
                          "anchors": chosen,
                          "unassigned_event_ids": [a.id for a in local if a.id not in ids],
                          "evidence": {"matched_fronts": len(matched),
                                       "available_fronts": len(local),
                                       "mean_absolute_shift_s": float(np.mean(
                                           [abs(x["shift_s"]) for x in chosen])),
                                       "nearby_context_fronts": sum(
                                           x["nearby_context_attack_distance_s"] is not None and
                                           x["nearby_context_attack_distance_s"] < .022
                                           for x in chosen)}}
            if key not in unique or cost < unique[key]["cost"]:
                unique[key] = hypothesis
        options = sorted(unique.values(), key=lambda option: option["cost"])
        for index, option in enumerate(options, 1):
            option["id"] = f"option_{index}"
            for a, b in zip(option["anchors"], option["anchors"][1:]):
                a["qn_to_next_anchor"] = b["target_qn"]-a["target_qn"]
        segment_id = f"bar_{bar}_beat_{beat}"
        winner = options[0] if options else None
        margin = options[1]["cost"]-winner["cost"] if len(options) > 1 else None
        if not local:
            state, reason = "unresolved", "no_attack_candidates_in_beat"
        elif not winner:
            state, reason = "unresolved", "no_grid_path_within_tolerance"
        elif len(winner["anchors"]) < 2 and not any(
                a["nearby_context_attack_distance_s"] is not None and
                a["nearby_context_attack_distance_s"] < .022 for a in winner["anchors"]):
            state, reason = "ambiguous", "single_attack_cannot_establish_subdivision"
        elif margin is not None and margin < .24:
            state, reason = "ambiguous", "competing_paths_have_similar_cost"
        elif any(a.confidence >= .65 and a.id in winner["unassigned_event_ids"] for a in local):
            state, reason = "ambiguous", "strong_front_left_unassigned"
        elif np.mean([a["confidence"] for a in winner["anchors"]]) < .32:
            state, reason = "ambiguous", "weak_attack_evidence"
        else:
            state, reason = "supported", "ordered_fronts_support_grid_path"
        segments.append({"id": segment_id, "bar": bar, "beat": beat,
                         "start_s": start, "end_s": finish,
                         "start_qn": begin, "end_qn": end, "status": state, "reason": reason,
                         "score_margin": margin,
                         "recommended_option": winner["id"] if state == "supported" else None,
                         "options": options[:3],
                         "observations": [{"event_id": a.id, "observed_s": a.time,
                                           "observed_qn": a.qn, "confidence": a.confidence,
                                           "novelty": a.novelty, "prominence": a.prominence,
                                           "energy_jump": a.energy_jump,
                                           **observations.get(a.id, {})} for a in local]})
    # Describe repeated relative onset shapes. These are corroborating cues,
    # never votes that turn an acoustically unresolved beat into a decision.
    repeats = {}
    for segment in segments:
        if not segment["options"]:
            continue
        top = segment["options"][0]["anchors"]
        if len(top) < 2:
            continue
        shape = tuple(round(a["target_qn"]-segment["start_qn"], 5) for a in top)
        repeats.setdefault(shape, []).append(segment["id"])
    for segment in segments:
        if segment["options"]:
            shape = tuple(round(a["target_qn"]-segment["start_qn"], 5)
                          for a in segment["options"][0]["anchors"])
            segment["similar_grid_patterns"] = [
                item for item in repeats.get(shape, ()) if item != segment["id"]][:8]
    return segments
