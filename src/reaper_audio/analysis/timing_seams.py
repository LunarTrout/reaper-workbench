"""Blind attack evidence and native 1x crossfade geometry."""
from __future__ import annotations

import numpy as np
from scipy import ndimage, signal

from ..errors import ValidationError

MAX_SOURCE_SKIP = .025
MAX_SOURCE_REUSE = .060
FADE_WIDTHS = (.004, .006, .008, .011, .015, .021)
PROFILES = {
    # minimum separation, lookback for the front, post-attack protection
    "auto": (.027, .013, .013),
    "picked": (.024, .011, .013),
    "percussion": (.021, .009, .012),
    "vocal": (.045, .027, .023),
    "sustained": (.042, .024, .022),
}


def attack_candidates(audio, profile, sensitivity, min_gap_s):
    """Find fronts without observing the grid. Preserve weak candidates for context."""
    if profile not in PROFILES:
        raise ValidationError("Unknown timing profile", details={"profile": profile})
    samples = np.asarray(audio.samples, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    rate = audio.sample_rate
    hop = max(1, round(rate / 1000))
    frames = len(samples) // hop
    if frames < 32:
        return []
    # Use energy across channels to avoid cancelling opposite-polarity inputs.
    scale = max(float(np.percentile(np.abs(samples), 93)), 1e-6) / .09
    x = samples / scale
    bands = []
    nyquist = rate / 2
    for lo, hi in ((45, 240), (240, 1600), (1600, 10500)):
        hi = min(hi, nyquist * .93)
        filt = (signal.sosfiltfilt(signal.butter(
            3, [lo, hi], btype="bandpass", fs=rate, output="sos"), x, axis=0)
                if hi > lo else np.zeros_like(x))
        power = np.mean(filt[:frames * hop].reshape(frames, hop, -1)**2, axis=(1, 2))
        bands.append(np.sqrt(ndimage.uniform_filter1d(power, 4)))
    energy = np.asarray(bands)
    compressed = np.log1p(energy / np.array([.003, .002, .0008])[:, None])
    rise = np.maximum(0, compressed - np.roll(compressed, 7, axis=1))
    rise[:, :7] = 0
    fft_size = max(64, 2 ** round(np.log2(rate * .012)))
    f, _, stft = signal.stft(x.T, fs=rate, nperseg=fft_size,
                             noverlap=fft_size-hop, axis=-1)
    spectrum = np.log1p(np.abs(stft[:, (f > 180) & (f < 9000), :frames]) / .00065)
    flux = np.maximum(0, spectrum - np.roll(spectrum, 6, axis=-1)).mean(axis=(0, 1))
    flux[:6] = 0
    novelty = np.array([.28, .34, .28]) @ rise + .65 * flux
    spacing, lookback, _ = PROFILES[profile]
    threshold = np.clip(sensitivity / 1.8, .5, 3.)
    peaks, props = signal.find_peaks(
        novelty, distance=max(1, round(rate / hop * max(spacing, min_gap_s))),
        prominence=.18 * threshold)
    events = []
    previous = -1
    for p, prominence in zip(peaks, props["prominences"]):
        if novelty[p] < .49 * threshold:
            continue
        lo = max(0, p - round(lookback * rate / hop))
        front = novelty[lo:p+1]
        under = np.flatnonzero(front < max(.13, np.min(front) + .25 * np.ptp(front)))
        at = min(p, lo + int(under[-1]) + 1) if under.size else lo
        if at <= previous:
            continue
        lead = energy[1:, max(0, at-12):at]
        follow = energy[1:, p:p+10]
        before = float(np.sqrt(np.mean(lead**2))) if lead.size else 0.
        after = float(np.sqrt(np.mean(follow**2)))
        if after < .0035:
            continue
        jump = float(np.log((after+.0015)/(before+.0015)))
        confidence = float(np.clip(.26*novelty[p] + .23*prominence + .09*jump, 0, 1))
        events.append({"id": f"evt_{len(events)+1:05d}", "source_s": at*hop/rate,
                       "sample": int(at*hop), "peak_s": p*hop/rate,
                       "kind": "syllable" if profile == "vocal" else "attack",
                       "confidence": confidence, "novelty": float(novelty[p]),
                       "prominence": float(prominence), "energy_jump": jump})
        previous = at
    return events


def select_anchors(events, grid, tolerance):
    """Follow local drift to identify a grid slot, then aim at the actual grid."""
    if not events or len(grid) < 2:
        return [], []
    observed = np.array([e["project_s"] for e in events])
    quality = np.array([e["confidence"] for e in events])
    renewal = np.array([e["energy_jump"] for e in events])
    grid = np.asarray(grid)
    step = float(np.median(np.diff(grid)))
    limit = min(tolerance, .49*step)
    # Restrict drift to the unambiguous side of half a subdivision.
    hypotheses = np.linspace(-limit, limit, 81)
    weights = np.where(quality >= .28, quality**2, 0.)
    width = max(.007, min(.013, step*.14))
    phase = []
    for focus in np.arange(grid[0], grid[-1]+.24, .24):
        local = np.abs(observed-focus) <= .59
        hits = observed[local]
        nearest = np.clip(np.searchsorted(grid, hits), 1, len(grid)-1)
        delta = hits - np.where(hits-grid[nearest-1] < grid[nearest]-hits,
                                grid[nearest-1], grid[nearest])
        distances = delta[:, None] - hypotheses[None, :]
        scores = weights[local] @ np.exp(-.5*(distances/width)**2)
        previous = phase[-1][1] if phase else 0.
        scores /= 1 + .55*((hypotheses-previous)/.026)**2
        bias = float(hypotheses[np.argmax(scores)]) if np.max(scores) > .18 else previous
        phase.append((float(focus), bias))
    phase_t, phase_d = np.asarray(phase).T
    anchors = []
    previous = -1
    for target, drift in zip(grid, np.interp(grid, phase_t, phase_d)):
        residual = observed-target
        distance = np.abs(residual-drift)
        candidates = np.flatnonzero(
            (distance < min(.36*step, .035)) & (np.abs(residual) <= limit) &
            (np.arange(len(events)) > previous) & (quality >= .18))
        if previous >= 0:
            candidates = candidates[observed[candidates] - observed[previous] > .49*step]
        if not candidates.size:
            continue
        merit = quality[candidates] + .065*np.clip(renewal[candidates], -.65, 1.8)
        merit -= .18*(distance[candidates]/max(.010, .26*step))**2
        best_n = int(np.argmax(merit))
        if merit[best_n] < .24:
            continue
        index = int(candidates[best_n])
        anchors.append({**events[index], "target_s": float(target),
                        "desired_shift_s": float(-residual[index])})
        previous = index
    return anchors, phase


def _group(anchors, strength, blocked, step):
    observed = np.array([a["project_s"] for a in anchors])
    wanted = np.array([a["desired_shift_s"]*strength for a in anchors])
    weight = np.clip([a["confidence"] for a in anchors], .25, .75)
    short = np.diff(observed, prepend=-10.) < 1.23*step
    weight /= np.where(short, .0045, .006)**2
    count = len(wanted)
    pins = {i: a["pin_shift_s"] for i, a in enumerate(anchors) if "pin_shift_s" in a}
    costs = np.full(count+1, np.inf)
    costs[0] = 0.
    edges = {}
    for end in range(1, count+1):
        if end in blocked:
            continue
        fixed = None
        for start in range(end-1, -1, -1):
            if start in pins:
                if fixed is not None and abs(fixed-pins[start]) > 1e-7:
                    break
                fixed = pins[start]
            if end-start > 18 and start in edges:
                break
            if not np.isfinite(costs[start]):
                continue
            w, d = weight[start:end], wanted[start:end]
            shift = fixed if fixed is not None else float(np.average(d, weights=w))
            cost = costs[start] + 1.6 + np.sum(w*(d-shift)**2)
            if cost < costs[end]:
                costs[end], edges[end] = cost, (start, end, shift)
    if count not in edges:
        raise ValidationError("No feasible seam between the retained boundaries")
    groups = []
    end = count
    while end:
        group = edges[end]
        groups.append(group)
        end = group[0]
    return list(reversed(groups))


def _seam(samples, rate, origin, left, right, a, b, protected, guard,
          *, project_bounds=None, source_bounds=None, guarded=()):
    # Candidate crossfade must follow the earlier attack and finish before the
    # next under both translations. Independent fronts also veto the seam.
    after = max(a+left, a+right) + guard
    before = min(b+left, b+right) - .006
    if project_bounds:
        after = max(after, project_bounds[0])
        before = min(before, project_bounds[1])
    if source_bounds:
        after = max(after, source_bounds[0]+max(left, right))
        before = min(before, source_bounds[1]+min(left, right))
    if before-after < FADE_WIDTHS[0]:
        return None
    pair_scale = 0.06
    best = None
    for width in FADE_WIDTHS:
        if not -MAX_SOURCE_REUSE <= left-right-width <= MAX_SOURCE_SKIP:
            continue
        n = round(width*rate)
        duration = n/rate
        if (duration > before-after or
                not -MAX_SOURCE_REUSE <= left-right-duration <= MAX_SOURCE_SKIP):
            continue
        ramp = np.linspace(0, 1, n)[:, None]
        aim = b+right-min(.034, (b-a)*.40)
        earliest = int(np.ceil(after*rate))
        latest = int(np.floor((before-duration)*rate))
        if latest < earliest:
            continue
        starts = np.linspace(earliest, latest,
                             min(28, 1+max(1, (latest-earliest)//max(1, round(.0025*rate)))),
                             dtype=int)
        for frame in np.unique(starts):
            start = float(frame/rate)
            end = start+duration
            raw_lo = start-max(left, right)
            raw_hi = end-min(left, right)
            if any(raw_lo < hi and raw_hi > lo for lo, hi in guarded):
                continue
            src_lo, src_hi = raw_lo-origin, raw_hi-origin
            if np.any((protected >= src_lo-.009) & (protected <= src_hi+.007)):
                continue
            i, j = round((start-left-origin)*rate), round((start-right-origin)*rate)
            if min(i, j) < 0 or max(i, j)+n > len(samples):
                continue
            x, y = samples[i:i+n], samples[j:j+n]
            blend = (1-ramp)*x+ramp*y
            goal = (1-ramp)*x*x+ramp*y*y
            chunk = max(2, round(rate*.0015))
            used = n//chunk*chunk
            e = np.sqrt(np.mean(blend[:used].reshape(-1, chunk, samples.shape[1])**2,
                                axis=1))
            wanted_e = np.sqrt(np.mean(goal[:used].reshape(-1, chunk, samples.shape[1]),
                                       axis=1))
            err = float(np.max(np.median(abs(20*np.log10((e+.001)/(wanted_e+.001))),
                                        axis=0)))
            rho = float(np.sum(x*y)/(np.linalg.norm(x)*np.linalg.norm(y)+1e-12))
            body = float(np.sqrt(np.mean(goal)))
            hf = float(np.sqrt(np.mean(np.diff(blend, axis=0)**2)))
            hf_goal = float(np.sqrt(np.mean((np.diff(x, axis=0)**2 +
                                            np.diff(y, axis=0)**2)*.5)))
            derivative = max(0., 20*np.log10((hf+.0004)/(hf_goal+.0004)))
            score = (err + .34*derivative + .9*max(0., -rho) +
                     2.5*np.log1p(body/pair_scale) + .4*abs(width-.008)/.009 +
                     1.25*abs(start+duration/2-aim)/max(.036, before-after))
            if best is None or score < best["score"]:
                best = {"start_s": start, "end_s": end, "score": float(score),
                        "blend_error_db": err, "correlation": rho,
                        "derivative_excess_db": float(derivative), "rms": body,
                        "source_delta_s": left-right-duration}
    return best


def plan_crossfades(audio, events, grid, *, position, offset, length, tolerance,
                    strength, profile, protected_ranges=(), explicit_targets=None,
                    clip_window=None, edge_fades=(0., 0.), fixed_targets=()):
    """Return scored seams and full-length, gap-free project clips at 1x."""
    samples = np.asarray(audio.samples, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    origin = position-offset
    first, last, first_shift, last_shift = clip_window or (position, position+length, 0., 0.)
    head_fade, tail_fade = edge_fades
    if first+head_fade >= last-tail_fade:
        raise ValidationError("No room to edit between the retained fades")
    left_gate = first+head_fade-first_shift
    right_gate = last-tail_fade-last_shift
    if right_gate <= left_gate:
        raise ValidationError("The local source window has no editable material")
    guarded = []
    for row in protected_ranges:
        if "source_start_s" in row:
            start, end = row["source_start_s"]+origin, row["source_end_s"]+origin
        else:
            start, end = row.get("start_s", row.get("project_start_s")), row.get(
                "end_s", row.get("project_end_s"))
        if start is None or end is None or not position-1e-7 <= start < end <= position+length+1e-7:
            raise ValidationError("Protected window needs valid times inside the item")
        start, end = max(start, position), min(end, position+length)
        if end > left_gate and start < right_gate:
            guarded.append((max(start, left_gate), min(end, right_gate)))
    if explicit_targets is None:
        anchors, phase = select_anchors(events, grid, tolerance)
        step = float(np.median(np.diff(grid))) if len(grid) >= 2 else .1
    else:
        anchors = [{**event, "target_s": explicit_targets[event["id"]],
                    "desired_shift_s": explicit_targets[event["id"]]-event["project_s"]}
                   for event in events if event["id"] in explicit_targets]
        intervals = np.diff([a["target_s"] for a in anchors])
        phase, step = [], float(np.median(intervals)) if len(intervals) else length
    anchors = [a for a in anchors if left_gate <= a["project_s"] <= right_gate]
    requested = set(fixed_targets)
    if requested - {a["id"] for a in anchors}:
        raise ValidationError("A fixed target lies past a retained fade")
    for a in anchors:
        if a["id"] in requested:
            a["pin_shift_s"] = a["desired_shift_s"]
    attack_times = np.array([a["source_s"] for a in anchors])
    extra = np.asarray([e["source_s"] for e in events if e["confidence"] >= .34
                        and (not len(attack_times) or
                             np.min(abs(attack_times-e["source_s"])) > .020)])

    def pin(name, time, shift):
        return {"id": name, "project_s": time, "source_s": time-origin,
                "target_s": time+shift, "confidence": 1., "desired_shift_s": shift,
                "pin": True, "pin_shift_s": shift}

    anchors = [pin("item_start", left_gate, first_shift), *anchors,
               *(pin(f"protected_{i}", (lo+hi)/2, 0.) for i, (lo, hi) in enumerate(guarded)),
               pin("item_end", right_gate, last_shift)]
    anchors.sort(key=lambda a: a["project_s"])
    blocked = set()
    guard = PROFILES[profile][2]
    cache = {}
    excluded = []
    while True:
        groups = _group(anchors, strength, blocked, step)
        seams = []
        for left, right in zip(groups, groups[1:]):
            a = anchors[left[1]-1]["project_s"]
            b = anchors[right[0]]["project_s"]
            key = (left[2], right[2], a, b)
            if key not in cache:
                cache[key] = _seam(
                    samples, audio.sample_rate, origin, *key, extra, guard,
                    project_bounds=(first+head_fade, last-tail_fade),
                    source_bounds=(left_gate, right_gate), guarded=guarded)
            seam = cache[key]
            if seam is None or abs(left[2]-right[2]) <= 1e-7:
                blocked.add(right[0])
                excluded.append({"between": [anchors[left[1]-1]["id"],
                                              anchors[right[0]]["id"]],
                                 "position_s": (a+b)/2,
                                 "reason": ("same_shift" if seam is not None else
                                            "keep_group_no_distinct_safe_seam")})
                break
            seams.append(seam)
        else:
            if any(one["end_s"] > two["start_s"] for one, two in zip(seams, seams[1:])):
                index = next(i for i,(one,two) in enumerate(zip(seams,seams[1:]))
                             if one["end_s"] > two["start_s"])
                blocked.add(groups[index+1][0])
                continue
            break
    blocks = []
    for i, (start, end, shift) in enumerate(groups):
        incoming = seams[i-1] if i else None
        outgoing = seams[i] if i < len(seams) else None
        left = incoming["start_s"] if incoming else first
        right = outgoing["end_s"] if outgoing else last
        fade_in = incoming["end_s"]-incoming["start_s"] if incoming else head_fade
        fade_out = outgoing["end_s"]-outgoing["start_s"] if outgoing else tail_fade
        for anchor in anchors[start:end]:
            anchor["achieved_shift_s"] = shift
            anchor["residual_s"] = anchor["project_s"]+shift-anchor["target_s"]
            anchor["fixed_by"] = [a["id"] for a in anchors[start:end] if "pin_shift_s" in a]
        source = left-origin-shift
        blocks.append({"position_s": left, "source_start_s": source,
                       "length_s": right-left, "rate": 1.,
                       "fade_in_s": fade_in, "fade_out_s": fade_out,
                       "event_ids": [a["id"] for a in anchors[start:end] if not a.get("pin")]})
    anchors = [a for a in anchors if not a.get("pin")]
    return {"blocks": blocks, "seams": seams, "anchors": anchors, "phase": phase,
            "diagnostics": excluded, "protected_front_count": len(extra),
            **timing_errors(anchors)}


def timing_errors(anchors):
    before = np.abs([a["desired_shift_s"] for a in anchors])
    after = np.abs([a["residual_s"] for a in anchors])
    return {f"{name}_error_{when}_s": float(np.percentile(values, percentile))
            if len(values) else None
            for name, percentile in (("median", 50), ("p90", 90))
            for when, values in (("before", before), ("after", after))}
