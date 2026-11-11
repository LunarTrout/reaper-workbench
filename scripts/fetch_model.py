"""Parallel range downloader for a pinned Hugging Face checkpoint (default: Ossicle-4B).

The HF CDN throttles a single connection to roughly 0.5 MB/s from here, while a dozen
parallel connections saturate the link, so each large file is fetched in fixed-size
ranges by a thread pool, written at its own offset into the final file, and resumed from
a sidecar of finished chunks. Every file is then checked against the size and sha256
pinned in manifest.json; a checksum failure drops that file's chunk state and re-fetches
it once. The manifest is generated from the Hub API, so a model whose remote code lives in
the checkpoint (trust_remote_code) gets every root file, not just the shards.

    python scripts/fetch_model.py                       # Ossicle-4B
    python scripts/fetch_model.py --verify
    python scripts/fetch_model.py --repo Audante/MOSS-Music-8B-Instruct-bnb-nf4 \
        --revision 91e222c03efb5320cf1bab1a39928307b9ceabdf \
        --dest work/models/moss-music-8b-nf4
    python scripts/fetch_model.py --workers 16
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = "SILVERTHRONE/Ossicle-4B"
REVISION = "d4def766b7465b31ee93853acd38964f699d652e"
LIB = Path(__file__).resolve().parents[1]
DEST = Path(os.environ.get("OSSICLE_MODEL_DIR", LIB / "work" / "models" / "ossicle-4b"))
MANIFEST = DEST / "manifest.json"
CHUNK = 8 * 1024 * 1024
STATE = DEST / ".chunks"
URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}/"
LICENSE = "apache-2.0"

_print_lock = threading.Lock()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def remote_files() -> dict[str, dict]:
    from huggingface_hub import HfApi

    info = HfApi().model_info(REPO, revision=REVISION, files_metadata=True)
    rows = {}
    for sib in info.siblings:
        if "/" in sib.rfilename:          # quantised vLLM variants and thinker/ live in subdirs
            continue
        lfs = getattr(sib, "lfs", None)
        rows[sib.rfilename] = {
            "size": sib.size,
            "sha256": (lfs or {}).get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None),
        }
    return rows


def write_manifest(files: dict[str, dict]) -> None:
    man = {"repo": REPO, "revision": REVISION, "license": LICENSE,
           "files": files,
           "total_bytes": sum((f.get("size") or 0) for f in files.values())}
    MANIFEST.write_text(json.dumps(man, indent=2, sort_keys=True), encoding="utf-8")


def _get_range(session, name: str, start: int, end: int, attempts: int = 6) -> bytes:
    """Inclusive byte range, retried with backoff."""
    last = None
    for attempt in range(attempts):
        try:
            r = session.get(URL + name, headers={"Range": f"bytes={start}-{end}"},
                            timeout=(20, 180), allow_redirects=True)
            if r.status_code in (200, 206):
                body = r.content
                want = end - start + 1
                if len(body) == want:
                    return body
                last = f"short body {len(body)}/{want}"
            elif r.status_code in (429, 500, 502, 503, 504):
                last = f"http {r.status_code}"
            else:
                last = f"http {r.status_code}: {r.text[:120]}"
        except Exception as exc:                      # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(min(2 ** attempt, 20) * 0.5)
    raise RuntimeError(f"{name} [{start}-{end}] failed: {last}")


def _state_path(name: str) -> Path:
    return STATE / (name.replace("/", "_") + ".json")


def download_file(name: str, size: int, workers: int, sessions) -> Path:
    target = DEST / name
    if size <= CHUNK:
        target.write_bytes(_get_range(sessions[0], name, 0, size - 1))
        return target

    STATE.mkdir(parents=True, exist_ok=True)
    sp = _state_path(name)
    done = set(json.loads(sp.read_text())) if sp.exists() else set()
    offsets = list(range(0, size, CHUNK))
    todo = [o for o in offsets if o not in done]

    if not target.exists() or target.stat().st_size != size:
        with target.open("wb") as fh:
            fh.truncate(size)

    lock = threading.Lock()
    tty = {"bytes": 0, "t0": time.time()}

    def work(offset: int, slot: int):
        end = min(offset + CHUNK, size) - 1
        body = _get_range(sessions[slot], name, offset, end)
        with lock:
            with target.open("r+b") as fh:
                fh.seek(offset)
                fh.write(body)
            done.add(offset)
            sp.write_text(json.dumps(sorted(done)))
            tty["bytes"] += len(body)
            elapsed = time.time() - tty["t0"]
            if elapsed > 15:
                rate = tty["bytes"] / elapsed / 1e6
                left = (len(offsets) - len(done)) * CHUNK / max(rate * 1e6, 1)
                with _print_lock:
                    print(f"  {name}: {len(done)}/{len(offsets)} chunks, "
                          f"{rate:.2f} MB/s, ~{left / 60:.0f} min left", flush=True)
                tty.update(bytes=0, t0=time.time())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, off, i % len(sessions)) for i, off in enumerate(todo)]
        for fut in as_completed(futures):
            fut.result()

    return target


def verify(files: dict[str, dict]) -> dict:
    rows, bad = [], []
    for name, meta in sorted(files.items()):
        path = DEST / name
        row = {"file": name, "expected": meta.get("size"), "exists": path.exists()}
        if path.exists():
            row["size"] = path.stat().st_size
            row["complete"] = row["size"] == row["expected"]
            if row["complete"] and meta.get("sha256"):
                row["sha256_match"] = sha256(path) == meta["sha256"]
        if not row.get("complete") or row.get("sha256_match") is False:
            bad.append(name)
        rows.append(row)
    return {"ok": not bad, "revision": REVISION, "incomplete": bad, "files": rows}


def main() -> int:
    import requests

    global REPO, REVISION, DEST, MANIFEST, STATE, URL, LICENSE

    ap = argparse.ArgumentParser(description="parallel range fetch of a pinned HF checkpoint")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--revision", default=REVISION)
    ap.add_argument("--dest", default=None, help="defaults to the Ossicle folder")
    ap.add_argument("--license", default=LICENSE)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    REPO, REVISION, LICENSE = args.repo, args.revision, args.license
    if args.dest:
        DEST = Path(args.dest)
    MANIFEST, STATE = DEST / "manifest.json", DEST / ".chunks"
    URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}/"

    DEST.mkdir(parents=True, exist_ok=True)
    files = remote_files()
    write_manifest(files)
    total = sum((f.get("size") or 0) for f in files.values())
    print(f"{len(files)} root files, {total / 1e9:.2f} GB, revision {REVISION}", flush=True)

    if args.verify:
        report = verify(files)
        print(json.dumps({"ok": report["ok"], "incomplete": report["incomplete"]}, indent=2))
        return 0 if report["ok"] else 1

    sessions = [requests.Session() for _ in range(max(1, args.workers))]
    for name in sorted(files, key=lambda n: -(files[n].get("size") or 0)):
        meta = files[name]
        if not meta.get("size"):
            print(f"skip {name}: unknown size", flush=True)
            continue
        path = DEST / name
        if path.exists() and path.stat().st_size == meta["size"]:
            if not meta.get("sha256") or sha256(path) == meta["sha256"]:
                print(f"have {name}", flush=True)
                continue
        t0 = time.time()
        print(f"fetch {name} ({meta['size'] / 1e6:.1f} MB)", flush=True)
        download_file(name, meta["size"], args.workers, sessions)
        ok = path.exists() and path.stat().st_size == meta["size"] and (
            not meta.get("sha256") or sha256(path) == meta["sha256"])
        print(f"  {name}: {path.stat().st_size if path.exists() else 0} bytes in "
              f"{time.time() - t0:.0f}s, sha256 {'ok' if ok else 'MISMATCH'}", flush=True)
        if not ok:
            _state_path(name).unlink(missing_ok=True)
            print("  re-fetching once", flush=True)
            download_file(name, meta["size"], args.workers, sessions)

    report = verify(files)
    print(json.dumps({"ok": report["ok"], "incomplete": report["incomplete"]}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
