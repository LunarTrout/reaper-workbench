"""Delta report: our mix against reference records, in numbers a mixer can act on.

The models describe; this measures. For every file it takes one 30 s window (the loudest
one by default, so a loud chorus is compared with a loud chorus rather than with an intro),
then prints the same ten numbers and the delta against the mean of the references, with a
plain-language direction for each delta.

Numbers, and what each one moves:
  lufs_i            integrated loudness of the window
  plr_db            peak-to-loudness: how much transient headroom the mix keeps
  crest_db          peak minus RMS: same idea, sample domain
  dynamism_db       p95-p10 of the 400 ms RMS curve: how much the level breathes
  low_ratio         <120 Hz share of the total energy
  lowmid_ratio      120-300 Hz share ("body" vs "mud")
  mid_ratio         300 Hz-2 kHz share
  presence_ratio    2-5 kHz share (guitar/vocal bite)
  air_ratio         8-16 kHz share
  centroid_hz       spectral centroid, a single brightness number
  side_mid_db       side over mid energy, the width of the picture
  correlation       L/R correlation, 1.0 = mono

    python scripts/reference_delta.py --ours work/preview_full_master.wav \
        --ref "C:/.../GRIMA_Silver_Storm.wav" --ref "C:/.../Wintersun_Winter_Madness.wav"
    python scripts/reference_delta.py --ours a.wav --ref b.wav --window 150:180
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

BANDS = {"low": (20, 120), "lowmid": (120, 300), "mid": (300, 2000),
         "presence": (2000, 5000), "air": (8000, 16000)}
WINDOW_S = 30.0

# direction hints: (metric, "high"|"low" meaning, text)
HINTS = {
    "plr_db": "если PLR ниже референса — микс переспрессован (bus/drum compression, limiting)",
    "crest_db": "низкий крест = мало транзиентов: проверь компрессию на барабанах и мастере",
    "dynamism_db": "низкий dynamism = ровная стена: верни движение секций и атаки",
    "low_ratio": "много низа — проверь саб/кик; мало — не хватает веса",
    "lowmid_ratio": "много 120-300 = мутность и «коробка», режь гитары/бас в этом месте",
    "mid_ratio": "мало середины — гитары/вокал уходят назад",
    "presence_ratio": "мало 2-5k — нет разборчивости и атаки; много — резкость",
    "air_ratio": "мало 8-16k — нет воздуха и тарелок; много — шипение",
    "centroid_hz": "центроид ниже — звук темнее референса, выше — ярче/жёстче",
    "side_mid_db": "ниже — микс уже референса, выше — шире (проверь моно-совместимость)",
    "correlation": "выше — микс ближе к моно; ниже — шире, но риск фазовых провалов",
    "lufs_i": "громкость окна: сравнивай после выравнивания, не гонись за цифрой",
}


def window(path: Path, forced: tuple[float, float] | None, window_s: float = WINDOW_S):
    info = sf.info(str(path))
    sr = info.samplerate
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    x = data[:, :2].T
    if forced:
        a, b = int(forced[0] * sr), int(forced[1] * sr)
        return x[:, a:b], sr, [forced[0], forced[1]]
    n = int(window_s * sr)
    if x.shape[1] <= n:
        return x, sr, [0.0, round(x.shape[1] / sr, 2)]
    hop = max(n // 2, 1)
    rms = [float(np.sqrt(np.mean(x[:, i:i + n] ** 2))) for i in range(0, x.shape[1] - n, hop)]
    best = int(np.argmax(rms)) * hop
    return x[:, best:best + n], sr, [round(best / sr, 2), round((best + n) / sr, 2)]


def band_energy(x: np.ndarray, sr: int, lo: float, hi: float) -> float:
    from scipy.signal import butter, sosfiltfilt
    nyq = sr / 2.0
    hi = min(hi, nyq * 0.99)
    sos = butter(4, [lo / nyq, hi / nyq], btype="band", output="sos")
    y = sosfiltfilt(sos, x, axis=1)
    return float(np.mean(y ** 2))


def measure(x: np.ndarray, sr: int) -> dict:
    import pyloudnorm as pyln

    total = float(np.mean(x ** 2)) + 1e-20
    peak = float(np.max(np.abs(x))) + 1e-20
    rms = float(np.sqrt(total))
    lufs = float(pyln.Meter(sr).integrated_loudness(np.ascontiguousarray(x.T)))
    crest = 20 * np.log10(peak / rms)
    plr = 20 * np.log10(peak) - lufs

    # 400 ms RMS curve: how much the level moves inside the window
    cell = max(int(0.4 * sr), 1)
    cells = [float(np.sqrt(np.mean(x[:, i:i + cell] ** 2)))
             for i in range(0, x.shape[1] - cell, cell)]
    cells_db = 20 * np.log10(np.array(cells) + 1e-12) if cells else np.array([-120.0])
    dynamism = float(np.percentile(cells_db, 95) - np.percentile(cells_db, 10))

    bands = {name: 10 * np.log10(band_energy(x, sr, lo, hi) / total + 1e-20)
             for name, (lo, hi) in BANDS.items()}

    left, right = x[0], x[1]
    mid, side = (left + right) / 2, (left - right) / 2
    side_mid = 10 * np.log10((np.mean(side ** 2) + 1e-20) / (np.mean(mid ** 2) + 1e-20))
    corr = float(np.dot(left, right) / ((np.linalg.norm(left) * np.linalg.norm(right)) + 1e-20))

    spec = np.abs(np.fft.rfft(x.mean(axis=0) * np.hanning(x.shape[1])))
    freqs = np.fft.rfftfreq(x.shape[1], 1 / sr)
    centroid = float(np.sum(spec * freqs) / (np.sum(spec) + 1e-20))

    return {"lufs_i": round(lufs, 2), "plr_db": round(float(plr), 2), "crest_db": round(crest, 2),
            "dynamism_db": round(dynamism, 2), "centroid_hz": round(centroid, 0),
            "side_mid_db": round(float(side_mid), 2), "correlation": round(corr, 3),
            **{f"{k}_ratio": round(v, 2) for k, v in bands.items()}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ours", required=True)
    ap.add_argument("--ref", action="append", required=True,
                    help="reference file, repeat for each record")
    ap.add_argument("--window", default=None, metavar="START:END",
                    help="forced window for our file, in seconds")
    ap.add_argument("--ref-window", default=None, metavar="START:END",
                    help="forced window for every reference")
    args = ap.parse_args()

    def parse(w):
        return None if not w else tuple(float(v) for v in w.split(":"))

    x, sr, span = window(Path(args.ours), parse(args.window))
    report = {"ours": {"path": args.ours, "span": span, **measure(x, sr)}, "references": []}
    for path in args.ref:
        xr, sr_r, span_r = window(Path(path), parse(args.ref_window))
        report["references"].append({"path": path, "span": span_r, **measure(xr, sr_r)})

    ours = report["ours"]
    keys = [k for k in ours if k not in ("path", "span")]
    mean_ref = {k: float(np.mean([r[k] for r in report["references"]])) for k in keys}
    delta = {k: round(ours[k] - mean_ref[k], 2) for k in keys}
    report["mean_reference"] = {k: round(v, 2) for k, v in mean_ref.items()}
    report["delta"] = delta

    print(f"our window {ours['span']} | references: "
          + ", ".join(f"{Path(r['path']).name[:28]} {r['span']}" for r in report["references"]))
    print(f"{'metric':<16}{'ours':>10}{'refs':>10}{'delta':>10}")
    for k in keys:
        print(f"{k:<16}{ours[k]:>10.2f}{mean_ref[k]:>10.2f}{delta[k]:>+10.2f}")
    print("\nгде смотреть:")
    for k, text in HINTS.items():
        if k in delta and abs(delta[k]) >= (1.0 if "ratio" in k or k in ("plr_db", "crest_db", "dynamism_db", "side_mid_db") else 100):
            print(f"  {k} {delta[k]:+.2f}: {text}")
    out = Path(args.ours).with_name("reference_delta.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nwritten:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
