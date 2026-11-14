"""Time-local level, spectral, pitch, transient and stereo feature profiles.

Each analysis cell describes measured signal properties. It does not assign an
instrument or prescribe an editing or mixing action.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import resample_poly

from .transients import detect_transients

FC = np.asarray([
    25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
    630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300,
    8000, 10000, 12500,
], dtype=float)
LO = FC * 10 ** (-1 / 20)
HI = FC * 10 ** (1 / 20)

PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_WINDOW_CACHE: dict[int, np.ndarray] = {}


def _hann_window(n: int) -> np.ndarray:
    """Reuse Hann windows across the thousands of equally sized cells."""
    window = _WINDOW_CACHE.get(n)
    if window is None:
        window = np.hanning(n)
        _WINDOW_CACHE[n] = window
    return window


def _linear_slope(times: np.ndarray, values: np.ndarray) -> float:
    """Least-squares slope for evenly sampled decay quarters.

    Equivalent to the historical first-degree ``np.polyfit`` call, without
    constructing a Vandermonde matrix for every cell and band.
    """
    times = np.asarray(times, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    t_mean = float(times.mean())
    v_mean = float(values.mean())
    denom = float(np.sum((times - t_mean) ** 2))
    if denom <= 0:
        return 0.0
    return float(np.sum((times - t_mean) * (values - v_mean)) / denom)



def _bands_matrix(nfft: int, fs: int) -> np.ndarray:
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    M = np.zeros((len(FC), len(freqs)))
    for i in range(len(FC)):
        if FC[i] >= fs / 2:
            continue
        m = (freqs >= LO[i]) & (freqs < HI[i])
        if not m.any():
            m[np.argmin(np.abs(freqs - FC[i]))] = True
        M[i] = m
    return M


_ERB_MATRIX_CACHE: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}


def _erb_matrix(nfft: int, fs: int) -> tuple[np.ndarray, np.ndarray]:
    """Reuse the fixed 24-band ERB masks for every temporal cell."""
    key = (nfft, fs)
    cached = _ERB_MATRIX_CACHE.get(key)
    if cached is not None:
        return cached
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    lo = ERB_FC / 10 ** (1 / 24)
    hi = ERB_FC * 10 ** (1 / 24)
    matrix = np.zeros((len(ERB_FC), len(freqs)), dtype=np.float64)
    any_band = np.zeros(len(ERB_FC), dtype=bool)
    for index, (a, b) in enumerate(zip(lo, hi)):
        mask = (freqs >= a) & (freqs < b)
        matrix[index] = mask
        any_band[index] = bool(mask.any())
    cached = (matrix, any_band)
    _ERB_MATRIX_CACHE[key] = cached
    return cached


def _decay_and_coherence(seg_l, seg_r, fs):
    """Compute decay and coherence from one quarter-spectrum pass.

    The historical helpers used the same quarter windows but computed the FFTs
    twice (once per channel for decay and once per channel for coherence).
    Reusing those spectra is exact when every quarter has the same length; the
    mixed-length edge case falls back to the original helpers.
    """
    channels = np.column_stack((seg_l, seg_r))
    parts = np.array_split(channels, 4, axis=0)
    lengths = {len(part) for part in parts}
    if any(len(part) < 32 for part in parts) or len(lengths) != 1:
        return _band_decay(seg_l, seg_r, fs), _coherence(seg_l, seg_r, fs)
    n = len(parts[0])
    window = _hann_window(n)
    f = np.fft.rfftfreq(n, 1.0 / fs)
    stacked = np.stack([part * window[:, None] for part in parts], axis=0)
    spectra = np.fft.rfft(stacked, axis=1)
    powers = np.mean(np.abs(spectra) ** 2, axis=2)
    edges = ((20, 200), (200, 2000), (2000, 16000))
    e_by_edge = []
    sxy_by_edge = []
    sxx_by_edge = []
    syy_by_edge = []
    for lo, hi in edges:
        mask = (f >= lo) & (f < hi)
        e = [float(power[mask].sum()) + 1e-20 for power in powers]
        left = spectra[:, :, 0]
        right = spectra[:, :, 1]
        sxy = (left[:, mask] * np.conj(right[:, mask])).sum()
        sxx = float((np.abs(left[:, mask]) ** 2).sum())
        syy = float((np.abs(right[:, mask]) ** 2).sum())
        e_by_edge.append(e)
        sxy_by_edge.append(sxy)
        sxx_by_edge.append(sxx)
        syy_by_edge.append(syy)
    decay = []
    for e in e_by_edge:
        db = 10 * np.log10(np.array(e))
        t = np.arange(len(db)) * (n / fs)
        slope = _linear_slope(t, db) if len(db) > 1 else 0.0
        decay.append(round(float(slope), 0))
    coherence = []
    for sxy, sxx, syy in zip(sxy_by_edge, sxx_by_edge, syy_by_edge):
        denom = sxx * syy
        coherence.append(round(float(abs(sxy) ** 2 / denom) if denom > 0 else 0.0, 3))
    return decay, coherence


def _active(x: np.ndarray, n: int = 2048) -> np.ndarray:
    """Самый громкий кусок клетки: там и живёт тон, а не в хвосте затухания."""
    if x.size <= n:
        return x
    # The original used a same-mode moving average.  Its maximum over full
    # windows is exactly the maximum of the window sums; prefix sums avoid the
    # O(N*n) direct convolution without changing the selected window.
    magnitudes = np.abs(x)
    csum = np.concatenate(([0.0], np.cumsum(magnitudes, dtype=np.float64)))
    window_sums = csum[n:] - csum[:-n]
    i = int(np.argmax(window_sums))
    return x[i:i + n]


def _f0_and_hnr(x: np.ndarray, fs: int, fmin: float = 40.0, fmax: float = 1200.0):
    """Основной тон и гармоничность по автокорреляции (через FFT)."""
    x = x - x.mean()
    if x.size < 256 or np.max(np.abs(x)) < 1e-6:
        return 0.0, 0.0
    n = 1 << int(np.ceil(np.log2(2 * x.size)))
    spec = np.fft.rfft(x, n)
    ac = np.fft.irfft(spec * np.conj(spec), n)[:x.size]
    if ac[0] <= 0:
        return 0.0, 0.0
    ac = ac / ac[0]
    lo = max(1, int(fs / fmax))
    hi = min(x.size - 2, int(fs / fmin))
    if hi <= lo:
        return 0.0, 0.0
    lag = lo + int(np.argmax(ac[lo:hi]))
    r = float(ac[lag])
    # параболическая подгонка вершины
    if 0 < lag < x.size - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        denom = (a - 2 * b + c)
        if abs(denom) > 1e-12:
            lag = lag + 0.5 * (a - c) / denom
    f0 = fs / lag if lag > 0 else 0.0
    hnr = 10 * np.log10(max(r, 1e-6) / max(1 - r, 1e-6))
    return float(f0), float(max(-20.0, min(40.0, hnr)))


def _chroma(freqs: np.ndarray, spec: np.ndarray) -> list:
    chr_ = np.zeros(12)
    m = (freqs >= 55) & (freqs <= 2000)
    if not m.any():
        return [0.0] * 12
    f = freqs[m]
    p = spec[m]
    pc = np.mod(np.round(12 * np.log2(f / 440.0) + 9).astype(int), 12)
    for i, c in enumerate(pc):
        chr_[c] += p[i]
    total = chr_.sum()
    if total <= 0:
        return [0.0] * 12
    chr_ = chr_ / total
    return [round(float(v), 3) for v in chr_]


def _peaks(spec, freqs, lo=200.0, hi=4000.0, n=3):
    """Формантоподобные пики: сглаженный спектр, локальные максимумы."""
    m = (freqs >= lo) & (freqs <= hi)
    if not m.any():
        return []
    f = freqs[m]
    p = np.log(spec[m] + 1e-20)
    k = max(3, len(p) // 60)
    smooth = np.convolve(p, np.ones(k) / k, mode="same")
    idx = [i for i in range(2, len(smooth) - 2)
           if smooth[i] > smooth[i - 1] and smooth[i] >= smooth[i + 1]]
    idx.sort(key=lambda i: smooth[i], reverse=True)
    out = []
    for i in idx[:n]:
        out.append(round(float(f[i]), 0))
    return sorted(out)


def _f0_hps(spec, freqs, fmin=45.0, fmax=1000.0):
    """Запасной путь: гармоническое произведение спектра (для коротких нот)."""
    m = (freqs >= fmin) & (freqs <= fmax)
    if not m.any():
        return 0.0
    f = freqs[m]
    p = spec[m] + 1e-20
    score = np.ones_like(p)
    for k in (1, 2, 3, 4):
        idx = np.searchsorted(freqs, f * k)
        idx = np.clip(idx, 0, len(spec) - 1)
        score *= spec[idx] + 1e-20
    return float(f[int(np.argmax(score))])


def _harmonics(spec, freqs, f0):
    """Сила гармоник 2f0…6f0 относительно f0 и доля негармонической энергии."""
    if f0 <= 20:
        return [], 0.0
    def band_power(fc, width=0.06):
        m = (freqs >= fc * (1 - width)) & (freqs <= fc * (1 + width))
        return float(spec[m].sum()) if m.any() else 0.0
    fund = band_power(f0)
    if fund <= 0:
        return [], 0.0
    levels = []
    tot_harm = fund
    for k in range(2, 7):
        p = band_power(f0 * k)
        levels.append(round(float(10 * np.log10((p + 1e-20) / (fund + 1e-20))), 1))
        tot_harm += p
    total = float(spec.sum()) + 1e-20
    inharm = float(1.0 - tot_harm / total)
    return levels, round(max(0.0, min(1.0, inharm)), 3)


def _band_decay(seg_l, seg_r, fs, nquart=4):
    """Спад энергии в low/mid/high внутри клетки, дБ/с."""
    channels = np.column_stack((seg_l, seg_r))
    parts = np.array_split(channels, nquart, axis=0)
    if any(len(p) < 32 for p in parts):
        return [0.0, 0.0, 0.0]
    n = min(len(p) for p in parts)
    edges = [(20, 200), (200, 2000), (2000, 16000)]
    vals = []
    for lo, hi in edges:
        e = []
        for p in parts:
            spectrum = np.fft.rfft(p[:n] * _hann_window(n)[:, None], axis=0)
            sp = np.mean(np.abs(spectrum) ** 2, axis=1)
            f = np.fft.rfftfreq(n, 1.0 / fs)
            m = (f >= lo) & (f < hi)
            e.append(float(sp[m].sum()) + 1e-20)
        db = 10 * np.log10(np.array(e))
        t = np.arange(len(db)) * (n / fs)
        slope = _linear_slope(t, db) if len(db) > 1 else 0.0
        vals.append(round(float(slope), 0))
    return vals


def _coherence(seg_l, seg_r, fs, edges=((20, 200), (200, 2000), (2000, 16000))):
    """Когерентность L/R по трём полосам: 1 — моно, 0 — независимо."""
    n = min(len(seg_l), len(seg_r))
    if n < 512:
        return [0.0, 0.0, 0.0]
    parts_l = np.array_split(seg_l[:n], 4)
    parts_r = np.array_split(seg_r[:n], 4)
    out = []
    f = np.fft.rfftfreq(len(parts_l[0]), 1.0 / fs)
    for lo, hi in edges:
        m = (f >= lo) & (f < hi)
        if not m.any():
            out.append(0.0)
            continue
        sxy = 0j
        sxx = 0.0
        syy = 0.0
        for pl, pr in zip(parts_l, parts_r):
            window = _hann_window(len(pl))
            L = np.fft.rfft(pl * window)
            R = np.fft.rfft(pr * window)
            sxy += (L[m] * np.conj(R[m])).sum()
            sxx += float((np.abs(L[m]) ** 2).sum())
            syy += float((np.abs(R[m]) ** 2).sum())
        denom = sxx * syy
        out.append(round(float(abs(sxy) ** 2 / denom) if denom > 0 else 0.0, 3))
    return out


def _window_rms(x: np.ndarray, win: int, hop: int) -> np.ndarray:
    """Windowed RMS for the exact start sequence used by the tape scanners."""
    if x.size == 0:
        return np.array([], dtype=float)
    starts = np.arange(0, max(1, x.size - win), hop, dtype=np.int64)
    if x.size < win:
        return np.asarray([float(np.sqrt(np.mean(x[0:win] ** 2)))])
    windows = np.lib.stride_tricks.sliding_window_view(x, win)[starts]
    return np.sqrt(np.mean(windows ** 2, axis=1))


def _modulation(x, fs: int) -> dict:
    """Биения/тремоло: доминирующая частота огибающей 2–12 Гц и её глубина."""
    x = np.asarray(x, dtype=float)
    if x.ndim == 2:
        x = np.sqrt(np.mean(x ** 2, axis=1))
    hop = max(1, int(0.005 * fs))
    win = max(32, int(0.02 * fs))
    env = _window_rms(x, win, hop).astype(float)
    if env.size < 16 or env.max() <= 0:
        return {"rate_hz": 0.0, "depth": 0.0}
    env = env - env.mean()
    spec = np.abs(np.fft.rfft(env * _hann_window(env.size)))
    freqs = np.fft.rfftfreq(env.size, hop / fs)
    m = (freqs >= 2) & (freqs <= 12)
    if not m.any():
        return {"rate_hz": 0.0, "depth": 0.0}
    i = int(np.argmax(spec[m]))
    rate = float(freqs[m][i])
    band = spec[m]
    depth = float(band[i] / (band.sum() + 1e-12))
    return {"rate_hz": round(rate, 2), "depth": round(depth, 3)}


def analyze_cell(seg_l: np.ndarray, seg_r: np.ndarray, fs: int = 44100,
                 prev_rms_db: float | None = None, M: np.ndarray | None = None,
                 nfft: int | None = None,
                 erb_matrix: np.ndarray | None = None,
                 erb_present: np.ndarray | None = None) -> dict:
    """Все признаки одного отрезка (клетки ленты)."""
    nfft = nfft or len(seg_l)
    if M is None:
        M = _bands_matrix(nfft, fs)
    win = _hann_window(nfft)
    channels = np.column_stack((seg_l, seg_r))
    if len(channels) < nfft:
        pad = nfft - len(channels)
        channels = np.pad(channels, ((0, pad), (0, 0)))
        seg_l = np.pad(seg_l, (0, pad))
        seg_r = np.pad(seg_r, (0, pad))

    channel_spectra = np.fft.rfft(channels * win[:, None], axis=0)
    spec = np.mean(np.abs(channel_spectra) ** 2, axis=1)
    spec *= 2.0 / (np.sum(win ** 2) * nfft)
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    power = M @ spec
    bands = [round(float(10 * np.log10(p + 1e-20)), 1) for p in power]

    rms = float(np.sqrt(np.mean(channels ** 2)))
    rms_db = 20 * np.log10(rms + 1e-12)
    peak = float(np.max(np.abs(channels)))
    crest_db = 20 * np.log10((peak + 1e-12) / (rms + 1e-12))

    zcr = float(np.mean(np.abs(np.diff(np.sign(channels), axis=0)) > 0))

    env = np.sqrt(np.mean(channels ** 2, axis=1))
    if env.size and env.max() > 1e-6:
        ipk = int(np.argmax(env))
        thr10, thr90 = 0.1 * env[ipk], 0.9 * env[ipk]
        i10 = int(np.argmax(env >= thr10))
        i90 = int(np.argmax(env >= thr90))
        attack_s = round(float(max(0, i90 - i10)) / fs, 4)
        tail = float(np.sqrt(np.mean(env[ipk:] ** 2))) if ipk < env.size else 0.0
        decay_db = round(float(20 * np.log10((peak + 1e-12) / (tail + 1e-12))), 1)
    else:
        attack_s, decay_db = 0.0, 0.0

    flux_db = round(float(rms_db - prev_rms_db), 1) if prev_rms_db is not None else 0.0

    psum = float(spec.sum()) + 1e-20
    centroid = float((freqs * spec).sum() / psum)
    spread = float(np.sqrt(((freqs - centroid) ** 2 * spec).sum() / psum))
    flatness = float(np.exp(np.mean(np.log(spec + 1e-20))) / (np.mean(spec) + 1e-20))
    cum = np.cumsum(spec)
    rolloff = float(freqs[int(np.searchsorted(cum, 0.85 * cum[-1]))])

    pitch_candidates = [_f0_and_hnr(_active(channel), fs)
                        for channel in (seg_l, seg_r)]
    f0, hnr = max(pitch_candidates, key=lambda item: item[1])
    if hnr < 0.0 or f0 <= 0:                 # автокорреляция не уверена — берём спектр
        alt = _f0_hps(spec, freqs)
        if alt > 0:
            f0 = alt
    chroma = _chroma(freqs, spec)
    harm, inharm = _harmonics(spec, freqs, f0)
    formants = _peaks(spec, freqs) if rms_db > -55 else []

    hits = 0
    energy = env
    if energy.size > 128 and energy.max() > 1e-5:
        k = max(3, int(0.003 * fs))
        e1 = np.convolve(energy, np.ones(k) / k, mode="same")
        thr = 0.5 * e1.max()
        idx = [i for i in range(1, e1.size - 1)
               if e1[i] > thr and e1[i] >= e1[i - 1] and e1[i] > e1[i + 1]]
        merged = []
        for i in idx:
            if not merged or i - merged[-1] > 0.008 * fs:
                merged.append(i)
        if len(merged) >= 3:
            gaps = np.diff(merged)
            if gaps.mean() > 0 and gaps.std() / gaps.mean() < 0.5:
                rate = fs / gaps.mean()
                if 6 <= rate <= 40:
                    hits = len(merged)

    a, b = seg_l - seg_l.mean(), seg_r - seg_r.mean()
    denom = (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12)
    corr = float((a * b).sum() / denom)
    side = (seg_l - seg_r) / 2.0
    width = float(np.sqrt(np.mean(side ** 2)) / (rms + 1e-12))
    el = float(np.sqrt(np.mean(seg_l ** 2)))
    er = float(np.sqrt(np.mean(seg_r ** 2)))
    pan = float((er - el) / (er + el + 1e-12))

    low = float(10 * np.log10(float(spec[(freqs >= 20) & (freqs < 200)].sum()) + 1e-20))
    midb = float(10 * np.log10(float(spec[(freqs >= 200) & (freqs < 2000)].sum()) + 1e-20))
    high = float(10 * np.log10(float(spec[(freqs >= 2000) & (freqs < 16000)].sum()) + 1e-20))

    decay_bands, coherence = _decay_and_coherence(seg_l, seg_r, fs)
    cell = {
        "rms_db": round(rms_db, 1), "peak": round(peak, 4), "crest_db": round(crest_db, 1),
        "attack_s": attack_s, "decay_db": decay_db, "flux_db": flux_db, "zcr": round(zcr, 3),
        "bands": bands,
        "centroid_hz": round(centroid, 1), "spread_hz": round(spread, 1),
        "flatness": round(flatness, 4), "rolloff_hz": round(rolloff, 1),
        "f0_hz": round(f0, 1),
        "note": (PITCH_NAMES[int(np.argmax(chroma))] if f0 > 0 else ""),
        "hnr_db": round(hnr, 1), "chroma": chroma,
        "harm": harm, "inharm": inharm, "formants": formants,
        "decay_bands": decay_bands,
        "coherence": coherence,
        "corr": round(corr, 3), "width": round(width, 3), "pan": round(pan, 3),
        "low_db": round(low, 1), "mid_db": round(midb, 1), "high_db": round(high, 1),
        "hits": hits,
    }
    cell["erb"] = _erb_bands(spec, freqs, erb_matrix, erb_present)
    cell["wall"] = _wall(cell)
    return cell


def technique(stereo: np.ndarray, fs: int = 44100, cell_s: float = 0.1) -> dict:
    """Отдельный проход 10 мс: видит тремоло/дробь, которые не влезают в клетку."""
    x = np.asarray(stereo, dtype=float)
    if x.ndim == 2:
        x = np.sqrt(np.mean(x ** 2, axis=1))
    hop = max(1, int(0.005 * fs))
    win = max(64, int(0.01 * fs))
    env = _window_rms(x, win, hop)
    if env.size < 8:
        return {"per_cell": [], "rate_hz": 0.0}
    d = np.diff(env, prepend=env[0])
    thr = 0.4 * (d.max() + 1e-12)
    peaks = [i for i in range(1, len(d) - 1)
             if d[i] > thr and d[i] >= d[i - 1] and d[i] > d[i + 1]]
    merged = []
    min_gap = int(0.025 / 0.005)
    for i in peaks:
        if not merged or i - merged[-1] > min_gap:
            merged.append(i)
    rate = 0.0
    if len(merged) >= 3:
        gaps = np.diff(merged) * 0.005
        if gaps.mean() > 0 and gaps.std() / gaps.mean() < 0.45:
            rate = float(1.0 / gaps.mean())
    per_cell = []
    if merged:
        times = np.array(merged) * 0.005
        idx = (times / cell_s).astype(int)
        per_cell = sorted(set(int(i) for i in idx))
    return {"per_cell": per_cell, "rate_hz": round(rate, 2),
            "fast": bool(6.0 <= rate <= 40.0)}


def analyze(stereo: np.ndarray, fs: int = 44100, hop_s: float = 0.1,
            nfft: int = 4096, loudness: dict | None = None) -> dict:
    """Compute time-local signal features and track-level summaries.

    ``loudness`` is an optional per-call reuse of already measured momentary
    blocks/integrated loudness/LRA; it never persists between calls.
    """
    x = np.asarray(stereo, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[0] == 0 or x.shape[1] == 0:
        raise ValueError("audio must be a non-empty samples-by-channels array")
    if fs <= 0 or hop_s <= 0 or nfft < 256:
        raise ValueError("fs/hop_s must be positive and nfft must be at least 256")
    if x.shape[1] > 2:
        raise ValueError("temporal profile requires mono/stereo audio or an explicit downmix")
    profile_audio = x
    has_stereo = x.shape[1] == 2
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    left, right = x[:, 0], x[:, 1]
    hop = max(1, int(round(hop_s * fs)))
    M = _bands_matrix(nfft, fs)
    erb_matrix, erb_present = _erb_matrix(nfft, fs)

    cells = []
    times = []
    prev = None
    for start in range(0, max(0, len(left) - hop // 2), hop):
        cell = analyze_cell(left[start:start + nfft], right[start:start + nfft], fs,
                            prev_rms_db=prev, M=M, nfft=nfft,
                            erb_matrix=erb_matrix, erb_present=erb_present)
        if not has_stereo:
            cell["coherence"] = None
            cell["corr"] = None
            cell["width"] = None
            cell["pan"] = None
        prev = cell["rms_db"]
        cells.append(cell)
        times.append(round(start / fs, 3))

    # производные по ленте: темп онсетов и «тремоло»
    flux = np.array([c["flux_db"] for c in cells])
    onsets = [i for i in range(1, len(flux)) if flux[i] > 6.0 and flux[i] >= flux[i - 1]]
    rate = 0.0
    if len(onsets) >= 2 and len(times) > 1:
        span = times[onsets[-1]] - times[onsets[0]]
        rate = (len(onsets) - 1) / span if span > 0 else 0.0

    mod = _modulation(x, fs)
    f0s = [c["f0_hz"] for c in cells if c["hnr_db"] > 3 and c["f0_hz"] > 20]
    jitter = 0.0
    if len(f0s) >= 3:
        m = float(np.mean(f0s))
        jitter = float(np.std(f0s) / m * 100.0) if m > 0 else 0.0
    out = {"hop_s": hop_s, "times": times, "fc": [float(v) for v in FC],
           "cells": cells, "onset_rate_hz": round(rate, 2),
           "onsets": len(onsets), "technique": technique(stereo, fs, hop_s),
           "mod_rate_hz": mod["rate_hz"], "mod_depth": mod["depth"],
           "jitter_pct": round(jitter, 2)}
    out.update(extras(profile_audio, fs, cells, loudness=loudness))
    return out


CHARS = " .:-=+*#%@"


def _shade(v: float, lo: float = -60.0, hi: float = 0.0) -> str:
    k = (v - lo) / (hi - lo)
    k = 0.0 if k < 0 else (1.0 if k > 1 else k)
    return CHARS[min(len(CHARS) - 1, int(k * (len(CHARS) - 1) + 0.5))]


def _curve(vals, lo, hi, chars=" ▁▂▃▄▅▆▇█") -> str:
    out = []
    for v in vals:
        k = (v - lo) / (hi - lo + 1e-9)
        k = 0.0 if k < 0 else (1.0 if k > 1 else k)
        out.append(chars[min(len(chars) - 1, int(k * (len(chars) - 1) + 0.5))])
    return "".join(out)


def picture(tp: dict, band_rows: int = 14, width: int = 100) -> str:
    """Лента как картинка: полосы по времени + кривые громкости, тона, панорамы.

    profile — the mapping returned by analyze().
    """
    cells = tp["cells"]
    if not cells:
        return "(пусто)"
    times = tp["times"]
    n = len(times)
    bands = [c["bands"] for c in cells]
    nb = len(bands[0])
    if width and n > width:                      # сжать по времени, если длинно
        k = int(np.ceil(n / width))
        bands = [[max(bands[i][j] for i in range(g, min(g + k, n))) for j in range(nb)]
                 for g in range(0, n, k)]
        times = [times[g] for g in range(0, n, k)]
        n = len(times)

    step = max(1, nb // band_rows)
    lines = []
    step_s = max(1, int(round(times[-1] / max(1, n - 1)))) if n > 1 else 1
    header = "".join(str(int(t)) if abs(t - round(t)) < 0.051 else " " for t in times)
    lines.append("время,с  " + header)
    for r in range(0, nb, step):
        grp = bands
        label = "%5.0fГц" % tp["fc"][r]
        row = "".join(
            _shade(max(bands[i][j] for j in range(r, min(r + step, nb)))) for i in range(n)
        )
        lines.append("%s |%s|" % (label, row))
    lines.append("      └" + "─" * n + "┘  шкала: ' '=-60 дБ … '@'=0 дБ")

    rms = [c["rms_db"] for c in cells]
    lines.append("RMS   " + _curve(rms, -60, -6) + "  %.1f…%.1f дБ" % (min(rms), max(rms)))
    cen = [c["centroid_hz"] for c in cells]
    lines.append("центр " + _curve([np.log10(max(v, 20)) for v in cen], np.log10(50), np.log10(12000))
                 + "  %.0f…%.0f Гц" % (min(cen), max(cen)))
    pan = [c["pan"] for c in cells if c.get("pan") is not None]
    if pan:
        lines.append("пан   " + _curve(pan, -1, 1, "Lll·rR") +
                     "  %.2f…%.2f" % (min(pan), max(pan)))
    corr = [c["corr"] for c in cells if c.get("corr") is not None]
    if corr:
        lines.append("corr  " + _curve(corr, -1, 1) +
                     "  %.2f…%.2f" % (min(corr), max(corr)))
    f0 = [c["f0_hz"] for c in cells]
    if max(f0) > 0:
        lines.append("f0    " + "".join("%-3s" % (c["note"] if c["hnr_db"] > 3 else "·")
                                        for c in cells))
    lines.append("онсеты: %d, %.1f Гц" % (tp.get("onsets", 0), tp.get("onset_rate_hz", 0.0)))
    return "\n".join(lines)


# ---------------------------------------------------------------- добавки к ленте

ERB_FC = np.geomspace(25.0, 12000.0, 24)


def _erb_bands(spec, freqs, matrix: np.ndarray | None = None,
               any_band: np.ndarray | None = None):
    """24 ERB-подобные полосы (геометрическая сетка 25 Гц…12 кГц)."""
    if matrix is not None and any_band is not None:
        values = matrix @ spec
        return [round(float(10 * np.log10(float(v) + 1e-20)), 1) if present else -200.0
                for v, present in zip(values, any_band)]
    out = []
    lo = ERB_FC / 10 ** (1 / 24)
    hi = ERB_FC * 10 ** (1 / 24)
    for a, b in zip(lo, hi):
        m = (freqs >= a) & (freqs < b)
        out.append(round(float(10 * np.log10(float(spec[m].sum()) + 1e-20)), 1) if m.any() else -200.0)
    return out


def _wall(cell) -> float:
    """«Стена»: доля середины в общем и шероховатость. 0 — прозрачно, 1 — плотно."""
    tot = 10 ** (cell["low_db"] / 10) + 10 ** (cell["mid_db"] / 10) + 10 ** (cell["high_db"] / 10)
    if tot <= 0:
        return 0.0
    share = 10 ** (cell["mid_db"] / 10) / tot
    rough = min(1.0, cell["flatness"] * 3)
    return round(float(min(1.0, 0.6 * share + 0.4 * rough)), 3)


def _loudness_blocks(stereo, fs, loudness: dict | None = None):
    """Measure 400 ms momentary and 3 s short-term blocks independently of LRA.

    ``loudness`` may carry the already-computed momentary blocks, integrated
    loudness and LRA from the caller.  The short-term 3 s blocks are still
    measured here because they are a distinct window size.
    """
    x = np.asarray(stereo, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    empty = np.array([], dtype=np.float64)
    if x.ndim != 2 or not 1 <= x.shape[1] <= 2:
        return {"lufs": None, "lra": None, "momentary": empty,
                "short_term": empty, "coverage": "unsupported_channel_count"}
    block_frames = max(1, int(round(0.4 * fs)))
    if len(x) < block_frames:
        return {"lufs": None, "lra": None, "momentary": empty,
                "short_term": empty, "coverage": "insufficient_for_400ms_momentary"}
    try:
        import pyloudnorm as pyln

        if loudness is not None:
            integrated = loudness.get("lufs")
            lra = loudness.get("lra")
            momentary = np.asarray(loudness.get("momentary", empty),
                                   dtype=np.float64)
            provided_short_term = loudness.get("short_term")
        else:
            momentary_meter = pyln.Meter(fs, block_size=0.4, overlap=0.75)
            integrated = float(momentary_meter.integrated_loudness(x))
            momentary = np.asarray(momentary_meter.blockwise_loudness,
                                   dtype=np.float64)
            lra = None
            provided_short_term = None
            if len(x) >= 3 * fs:
                lra_meter = pyln.Meter(fs)
                lra_value = float(lra_meter.loudness_range(x))
                lra = lra_value if np.isfinite(lra_value) else None
        short_term = (np.asarray(provided_short_term, dtype=np.float64)
                      if provided_short_term is not None else empty)
        if not short_term.size and len(x) >= 3 * fs:
            short_meter = pyln.Meter(fs, block_size=3.0, overlap=0.97)
            short_meter.integrated_loudness(x)
            short_term = np.asarray(short_meter.blockwise_loudness,
                                    dtype=np.float64)
    except (ImportError, ValueError, FloatingPointError):
        return {"lufs": None, "lra": None, "momentary": empty,
                "short_term": empty, "coverage": "unavailable"}
    momentary = np.nan_to_num(momentary, nan=-200.0, neginf=-200.0, posinf=0.0)
    short_term = np.nan_to_num(short_term, nan=-200.0, neginf=-200.0, posinf=0.0)
    lufs = integrated if integrated is not None and np.isfinite(integrated) else None
    lra = lra if lra is not None and np.isfinite(lra) else None
    if lufs is None:
        coverage = "silent_or_unmeasurable"
    elif len(x) < 3 * fs:
        coverage = "momentary_and_integrated_only: under_3s_for_short_term_and_lra"
    elif len(x) < 60 * fs:
        coverage = "measured: LRA stability not established under 60s"
    else:
        coverage = "complete"
    return {"lufs": lufs, "lra": lra, "momentary": momentary,
            "short_term": short_term, "coverage": coverage}


def _harmony(cells):
    """Тональный центр: сумма хромы и близость к мажорному/минорному профилю."""
    acc = np.zeros(12)
    for c in cells:
        ch = c.get("chroma") or []
        if len(ch) == 12:
            acc += np.array(ch)
    if acc.sum() <= 0:
        return {"pitch_classes": [0.0] * 12, "major": 0.0, "minor": 0.0, "top": []}
    acc = acc / acc.sum()
    major = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
    minor = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
    def best(tpl):
        if np.std(acc) < 1e-12:
            return None, None
        vals = [float(np.corrcoef(np.roll(tpl, k), acc)[0, 1]) for k in range(12)]
        k = int(np.argmax(vals))
        return round(float(vals[k]), 3), k
    maj, maj_k = best(major)
    mnr, mnr_k = best(minor)
    top = [int(i) for i in np.argsort(acc)[::-1][:3]]
    return {"pitch_classes": [round(float(v), 3) for v in acc],
            "major": maj, "major_root": maj_k,
            "minor": mnr, "minor_root": mnr_k, "top": top}


def _sii(cells) -> float:
    """Грубая разборчивость: доля речи-полос (500–4000) над общим уровнем."""
    num = 0.0
    den = 0.0
    for c in cells:
        if c["rms_db"] < -60:
            continue
        b = c["bands"]
        f = FC
        speech = [v for v, hz in zip(b, f) if 500 <= hz <= 4000]
        allb = [v for v in b]
        if not speech:
            continue
        num += float(np.mean([10 ** (v / 10) for v in speech]))
        den += float(np.mean([10 ** (v / 10) for v in allb]))
    if den <= 0:
        return 0.0
    return round(float(min(1.0, num / den)), 3)


def extras(stereo, fs: int, cells: list, loudness: dict | None = None) -> dict:
    """Add loudness, tonal, speech-proxy and signal-check summaries to the tape."""
    x = np.asarray(stereo, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    loud = _loudness_blocks(x, fs, loudness=loudness)
    momentary = loud["momentary"]
    short_term = loud["short_term"]
    for i, c in enumerate(cells):
        if i < len(momentary):
            c["momentary_lufs"] = round(float(momentary[i]), 1)
        if i < len(short_term):
            c["short_term_lufs"] = round(float(short_term[i]), 1)
    channel_dc = np.mean(x, axis=0)
    dc = float(np.max(np.abs(channel_dc)))
    clip = int(np.count_nonzero(np.abs(x) >= 0.999))
    corr = None
    if x.shape[1] == 2 and np.std(x[:, 0]) > 0 and np.std(x[:, 1]) > 0:
        candidate = float(np.corrcoef(x[:, 0], x[:, 1])[0, 1])
        if np.isfinite(candidate):
            corr = candidate
    return {"lufs": loud["lufs"], "lra": loud["lra"],
            "sii_proxy": _sii(cells), "harmony": _harmony(cells),
            "checks": {"dc": round(dc, 6),
                       "dc_by_channel": [round(float(value), 6) for value in channel_dc],
                       "clip_samples": clip,
                       "mono_corr": round(corr, 3) if corr is not None else None},
            "loudness_coverage": loud["coverage"]}


def transients_stats(stereo, fs: int = 44100) -> dict:
    """Return signal-level transient candidates and their measured evidence."""
    x = np.asarray(stereo, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    from ..models import AudioBuffer

    return detect_transients(AudioBuffer(x, fs))


def masking(tracks: dict, min_db: float = 10.0, top: int = 8,
            floor_db: float = -80.0) -> dict:
    """Кто кого перекрывает: сравнение одновременных активных клеток.

    tracks: {id: {"name": str, "cells": [{"bands": [...]}, ...]}}.
    Полосы сравниваются только там, где обе дорожки реально звучат; тишина
    не превращается в «маскирование» на 170 дБ.
    """
    if not isinstance(tracks, dict):
        raise ValueError("tracks must map source IDs to cell data")
    if not np.isfinite(min_db) or min_db < 0 or not isinstance(top, int) or top < 1:
        raise ValueError("min_db must be finite/non-negative and top must be positive")
    if not np.isfinite(floor_db):
        raise ValueError("floor_db must be finite")
    series = {}
    for key, item in tracks.items():
        cells = item.get("cells") or []
        if not cells:
            continue
        raw = np.array([c["bands"] for c in cells], dtype=float)
        if raw.ndim != 2 or raw.size == 0:
            continue
        raw = np.where(np.isfinite(raw), raw, floor_db)
        # Compare tonal balance, not fader level: centre each cell by its own
        # broadband mean. Keep the raw levels for the active-band test, so a
        # silent band in one source is not treated as a 70 dB difference.
        normalized = raw - raw.mean(axis=1, keepdims=True)
        series[key] = (raw, normalized, item.get("name") or str(key))
    if len(series) < 2:
        return {"pairs": [], "note": "нужно ≥2 дорожек"}
    pairs = []
    keys = list(series)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            left_raw, left, na = series[a]
            right_raw, right, nb = series[b]
            count = min(len(left), len(right))
            if count == 0:
                continue
            diff = left[:count] - right[:count]
            valid = (left_raw[:count] > floor_db) & (right_raw[:count] > floor_db)
            band_count = diff.shape[1]
            median = np.full(band_count, np.nan)
            coverage = np.zeros(band_count, dtype=int)
            for band in range(band_count):
                values = diff[valid[:, band], band]
                if values.size:
                    median[band] = float(np.median(values))
                    coverage[band] = int(values.size)
            if not np.any(np.isfinite(median)):
                continue
            k = int(np.nanargmax(median))
            if median[k] >= min_db:
                pairs.append({"masker": na, "target": nb,
                              "band_hz": float(FC[k]), "db": round(float(median[k]), 1),
                              "cells": coverage[k]})
            k2 = int(np.nanargmin(median))
            if -median[k2] >= min_db:
                pairs.append({"masker": nb, "target": na,
                              "band_hz": float(FC[k2]), "db": round(float(-median[k2]), 1),
                              "cells": coverage[k2]})
    pairs.sort(key=lambda p: -p["db"])
    return {"pairs": pairs[:top], "count": len(pairs), "min_db": min_db,
            "floor_db": floor_db,
            "interpretation": "same-cell, active-band median difference"}
