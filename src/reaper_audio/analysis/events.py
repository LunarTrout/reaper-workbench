"""Short-window signal events complementary to the temporal feature profile.

Returns onset, level-spike and clipped-sample evidence with timestamps and
local signal measurements. Event labels describe detector rules, not musical
roles or recommended edits.
"""
from __future__ import annotations

from bisect import bisect_left

import numpy as np

HOP_S = 0.002          # 2 мс — хватает на 32-е при 300 BPM (37 мс)
WIN_S = 0.004          # окно огибающей 4 мс


def _has_near_event(event_times: list[float], candidate_t: float,
                    tolerance_s: float = 0.05) -> bool:
    """Exact nearest-neighbour replacement for a linear event scan."""
    pos = bisect_left(event_times, candidate_t)
    return ((pos < len(event_times) and
             abs(candidate_t - event_times[pos]) < tolerance_s) or
            (pos > 0 and abs(candidate_t - event_times[pos - 1]) < tolerance_s))


def _envelope(samples: np.ndarray, fs: int, hop_s: float = HOP_S,
              win_s: float = WIN_S):
    """Build an energy-preserving envelope without summing channels in phase."""
    data = np.asarray(samples, dtype=float)
    if data.ndim == 1:
        data = data[:, None]
    hop = max(1, int(hop_s * fs))
    win = max(hop, int(win_s * fs))
    n = max(1, (len(data) - win) // hop + 1)
    if len(data) < win:
        # Keep the historical short-input behavior: the one window is shorter
        # than win and is measured as-is.
        peaks = np.zeros(1)
        rmss = np.zeros(1)
        segments = [data[0:win]]
        peaks[0] = np.max(np.abs(segments[0]))
        rmss[0] = np.sqrt(np.mean(segments[0] ** 2))
    else:
        windows = np.lib.stride_tricks.sliding_window_view(data, win, axis=0)
        windows = windows[::hop][:n]
        peaks = np.max(np.abs(windows), axis=(1, 2))
        rmss = np.sqrt(np.mean(windows ** 2, axis=(1, 2)))
    times = np.arange(n) * hop / fs
    return times, peaks, rmss


def _event_spectrum(stereo: np.ndarray, fs: int, t0: float, dur: float) -> dict:
    """Average channel power in a short event window, with no phase cancellation."""
    a = int(t0 * fs)
    b = min(len(stereo), a + max(128, int(dur * fs)))
    seg = stereo[a:b]
    if seg.ndim == 1:
        seg = seg[:, None]
    if len(seg) < 128:
        return {"band": "?", "centroid_hz": 0.0, "bands_db": []}
    spec_channels = np.fft.rfft(seg * np.hanning(len(seg))[:, None], axis=0)
    spec = np.mean(np.abs(spec_channels) ** 2, axis=1)
    f = np.fft.rfftfreq(len(seg), 1.0 / fs)
    e = [float(spec[(f >= 20) & (f < 200)].sum()),
         float(spec[(f >= 200) & (f < 2000)].sum()),
         float(spec[(f >= 2000) & (f < 16000)].sum())]
    tot = sum(e) + 1e-20
    cent = float((f * spec).sum() / (spec.sum() + 1e-20))
    return {"band": ["low", "mid", "high"][int(np.argmax(e))],
            "centroid_hz": round(cent, 0),
            "bands_db": [round(10 * np.log10(v / tot + 1e-20), 1) for v in e]}


def _event_spectra(stereo: np.ndarray, fs: int, times: list[float],
                   dur: float = 0.04, chunk: int = 512) -> list[dict]:
    """The same per-event spectrum as :func:`_event_spectrum`, in bounded batches.

    The event enrichment loop used to call one small FFT per onset; a dense
    master can have thousands.  Batching rows changes no value: each row is the
    same window, Hann taper, rfft and band sums.  Chunking bounds the temporary
    matrix so eight analysis workers cannot multiply the peak memory.
    """
    length = max(128, int(dur * fs))
    results: list[dict] = [{} for _ in times]
    full: list[tuple[int, int]] = []
    for index, t0 in enumerate(times):
        a = int(t0 * fs)
        b = min(len(stereo), a + length)
        if b - a >= length:
            full.append((index, a))
        else:
            results[index] = _event_spectrum(stereo, fs, t0, dur)
    if not full:
        return results
    window = np.hanning(length)
    frequencies = np.fft.rfftfreq(length, 1.0 / fs)
    low_mask = (frequencies >= 20) & (frequencies < 200)
    mid_mask = (frequencies >= 200) & (frequencies < 2000)
    high_mask = (frequencies >= 2000) & (frequencies < 16000)
    for start in range(0, len(full), chunk):
        batch = full[start:start + chunk]
        indices = np.asarray([index for index, _ in batch], dtype=np.int64)
        starts = np.asarray([offset for _, offset in batch], dtype=np.int64)
        rows = starts[:, None] + np.arange(length, dtype=np.int64)[None, :]
        segments = stereo[rows]
        spectra = np.fft.rfft(segments * window[None, :, None], axis=1)
        power = np.mean(np.abs(spectra) ** 2, axis=2)
        low = power[:, low_mask].sum(axis=1)
        mid = power[:, mid_mask].sum(axis=1)
        high = power[:, high_mask].sum(axis=1)
        total = low + mid + high + 1e-20
        centroid = (power * frequencies).sum(axis=1) / (
            power.sum(axis=1) + 1e-20)
        for row, index in enumerate(indices):
            bands = [float(low[row]), float(mid[row]), float(high[row])]
            results[int(index)] = {
                "band": ["low", "mid", "high"][int(np.argmax(bands))],
                "centroid_hz": round(float(centroid[row]), 0),
                "bands_db": [round(10 * np.log10(value / total[row] + 1e-20), 1)
                             for value in bands],
            }
    return results


def _band_energy(stereo: np.ndarray, fs: int, t0: float, dur: float):
    a = int(t0 * fs)
    b = min(len(stereo), a + max(64, int(dur * fs)))
    seg = stereo[a:b]
    if seg.ndim == 1:
        seg = seg[:, None]
    if len(seg) < 64:
        return "?"
    spec_channels = np.fft.rfft(seg * np.hanning(len(seg))[:, None], axis=0)
    spec = np.mean(np.abs(spec_channels) ** 2, axis=1)
    f = np.fft.rfftfreq(len(seg), 1.0 / fs)
    e = [float(spec[(f >= 20) & (f < 200)].sum()),
         float(spec[(f >= 200) & (f < 2000)].sum()),
         float(spec[(f >= 2000) & (f < 16000)].sum())]
    return ["low", "mid", "high"][int(np.argmax(e))]


def _min_tree(values: np.ndarray) -> tuple[np.ndarray, int]:
    """Segment tree of minima for exact first-at-or-below queries."""
    count = len(values)
    size = 1
    while size < count:
        size <<= 1
    tree = np.full(2 * size, np.inf, dtype=np.float64)
    tree[size:size + count] = values
    for node in range(size - 1, 0, -1):
        tree[node] = min(tree[node * 2], tree[node * 2 + 1])
    return tree, size


def _first_at_or_below(tree: np.ndarray, size: int, count: int,
                       start: int, threshold: float) -> int:
    """First index >= start whose value is <= threshold, or count."""
    if start >= count:
        return count

    def descend(node: int, low: int, high: int) -> int:
        if high <= start or tree[node] > threshold:
            return count
        if high - low == 1:
            return low if low < count else count
        middle = (low + high) // 2
        found = descend(node * 2, low, middle)
        if found < count:
            return found
        return descend(node * 2 + 1, middle, high)

    return descend(1, 0, size)


def detect_events(stereo: np.ndarray, fs: int = 44100, hop_s: float = HOP_S,
                  onset_db: float = 5.0, refractory_s: float = 0.012,
                  spike_db: float = 15.0, spike_window_s: float = 0.02,
                  floor_db: float = -70.0) -> dict:
    """Detect short-window onset, spike and clipping evidence."""
    x = np.asarray(stereo, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[0] == 0 or x.shape[1] == 0:
        raise ValueError("audio must be a non-empty samples-by-channels array")
    if (fs <= 0 or not np.isfinite(hop_s) or hop_s <= 0 or
            onset_db < 0 or refractory_s < 0 or spike_db < 0 or
            spike_window_s <= 0 or not np.isfinite(floor_db)):
        raise ValueError("event detector parameters are outside their valid ranges")
    times, peaks, rmss = _envelope(x, fs, hop_s)
    if len(times) == 0:
        return {"hop_ms": hop_s * 1000, "events": [], "count": 0}

    db = 20 * np.log10(peaks + 1e-12)
    duration_tree, duration_size = _min_tree(db)
    events = []

    # --- онсеты: рост уровня после спада
    # The original scan visits every 2 ms hop.  Its comparisons are pure array
    # facts; compute the candidate mask once, then apply the sequential
    # refractory rule only to the candidates in the same order.
    rise = np.empty_like(db)
    rise[0] = -np.inf
    rise[1:] = db[1:] - db[:-1]
    d1 = np.concatenate(([np.inf], db[:-1]))
    d2 = np.concatenate(([np.inf, np.inf], db[:-2]))
    d3 = np.concatenate(([np.inf, np.inf, np.inf], db[:-3]))
    prev_dip = np.minimum(np.minimum(d1, d2), d3)
    onset_mask = ((db >= floor_db) & (rise >= onset_db) &
                  (db > prev_dip + onset_db))
    last_t = -1e9
    for i in np.flatnonzero(onset_mask):
        candidate_t = float(times[i])
        if (candidate_t - last_t) < refractory_s:
            continue
        last_t = candidate_t
        events.append({"t": round(candidate_t, 3), "kind": "onset",
                       "level_db": round(float(db[i]), 1)})

    # --- пики и длительность каждого события
    spectra = _event_spectra(x, fs, [ev["t"] for ev in events], 0.04)
    for ev, spec in zip(events, spectra):
        i0 = int(ev["t"] / hop_s)
        i1 = min(len(db), i0 + int(0.15 / hop_s))
        win = db[i0:i1]
        if len(win) == 0:
            continue
        pk_i = i0 + int(np.argmax(win))
        pk_db = float(db[pk_i])
        # длительность: пока не упадёт на 15 дБ ниже пика
        j = _first_at_or_below(duration_tree, duration_size, len(db),
                               pk_i, pk_db - 15)
        dur = (j - pk_i) * hop_s
        # крест внутри события
        seg_p = peaks[i0:i1]
        seg_r = rmss[i0:i1]
        crest = 20 * np.log10((seg_p.max() + 1e-12) / (seg_r.mean() + 1e-12))
        capped = j >= len(db)
        ev.update({"peak_db": round(pk_db, 1),
                   "dur_ms": round(min(dur, 0.15) * 1000, 0),
                   "dur_capped": bool(capped),
                   "crest_db": round(float(crest), 1),
                   "band": spec["band"],
                   "centroid_hz": spec["centroid_hz"],
                   "bands_db": spec["bands_db"]})

    # --- скачки громкости (внезапно громко)
    w = max(1, int(spike_window_s / hop_s))
    event_times = sorted(ev["t"] for ev in events)
    if len(db) > w:
        jump = db[w:] - db[:-w]
        spike_mask = (jump >= spike_db) & (db[w:] > -40)
        for offset in np.flatnonzero(spike_mask):
            i = int(offset) + w
            candidate_t = float(times[i])
            if _has_near_event(event_times, candidate_t):
                continue
            rounded_t = round(candidate_t, 3)
            events.append({"t": rounded_t, "kind": "spike",
                           "level_db": round(float(db[i]), 1),
                           "jump_db": round(float(jump[offset]), 1)})
            event_times.insert(bisect_left(event_times, rounded_t), rounded_t)

    # --- перегруз
    clip_idx = np.where(np.any(np.abs(x) >= 0.999, axis=1))[0]
    if clip_idx.size:
        events.append({"t": round(float(clip_idx[0]) / fs, 3), "kind": "clip",
                       "count": int(np.count_nonzero(np.abs(x) >= 0.999))})

    events.sort(key=lambda e: e["t"])
    for ev in events:
        if ev["kind"] == "onset":
            idx = int(ev["t"] / hop_s)
            if idx < len(rmss):
                ev["rms_db"] = round(float(20 * np.log10(rmss[idx] + 1e-12)), 1)

    ons = [e["t"] for e in events if e["kind"] == "onset"]
    rate = 0.0
    regular = 0.0
    if len(ons) >= 3:
        gaps = np.diff(ons)
        span = ons[-1] - ons[0]
        rate = (len(ons) - 1) / span if span > 0 else 0.0
        regular = float(np.std(gaps) / (np.mean(gaps) + 1e-9))
    peaks_db = [e.get("peak_db", e.get("level_db", -120)) for e in events]
    return {"hop_ms": round(hop_s * 1000, 2), "events": events, "count": len(events),
            "max_peak_db": round(float(max(peaks_db)), 1) if peaks_db else None,
            "onsets": len(ons), "onset_rate_hz": round(rate, 2),
            "regularity": round(regular, 3),
            "min_gap_ms": round(float(np.min(np.diff(ons))) * 1000, 1) if len(ons) > 1 else None,
            "spikes": sum(1 for e in events if e["kind"] == "spike"),
            "clips": sum(1 for e in events if e["kind"] == "clip")}
