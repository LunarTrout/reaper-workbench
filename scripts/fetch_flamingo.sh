#!/usr/bin/env bash
# Fetch Music Flamingo GGUF weights (resumes partial downloads) and verify SHA-256.
# Weights are NVIDIA OneWay Noncommercial Research — never commit them.
set -euo pipefail

DEST="${1:-$HOME/Documents/Codex/model-store/music-flamingo-q5_1}"
REV="a059053433697011c6928b1962110040f4bcb4d0"
BASE="https://huggingface.co/henry1477/music-flamingo-gguf/resolve/$REV"

MODEL="music-flamingo-Q5_1.gguf"
MMPROJ="mmproj-music-flamingo-bf16.gguf"
MODEL_SHA="0742a216d86e28c5ecde1bf3bce6261d509edd540e2512a056684ddcc31051a6"
MMPROJ_SHA="d4be69ed65f25dae97062febd44f9f41c0f6b14178f1cfb530fd894d595a4f94"

mkdir -p "$DEST"
cd "$DEST"

fetch() {
  local name="$1"
  echo "== $name"
  curl -L --retry 5 --retry-delay 5 -C - -o "$name" "$BASE/$name"
}

verify() {
  local name="$1" want="$2"
  local got
  got=$(sha256sum "$name" | cut -d' ' -f1)
  if [ "$got" != "$want" ]; then
    echo "SHA-256 mismatch for $name: $got != $want" >&2
    return 1
  fi
  echo "ok $name $got"
}

[ -f "$MODEL" ]  || fetch "$MODEL"
[ -f "$MMPROJ" ] || fetch "$MMPROJ"

# resume any partial file that exists but is short
fetch "$MODEL"
fetch "$MMPROJ"

verify "$MODEL" "$MODEL_SHA"
verify "$MMPROJ" "$MMPROJ_SHA"
echo "weights ready in $DEST"
