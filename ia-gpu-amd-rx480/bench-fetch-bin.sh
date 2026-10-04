#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bench-fetch-bin.sh -- baja el llama-bench upstream (Vulkan x64) a bench-bin/.
#
# bench-bin/ NO se versiona: son ~86 MB y el .gitignore excluye *.so/*.bin, asi
# que un commit dejaria el binario roto. Se monta read-only en el contenedor
# como /opt/llama-bench. bench-run.sh lo llama solo si falta.
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG=b11382
DEST="${HERE}/bench-bin"
BIN="${DEST}/llama-${TAG}/llama-bench"

if [ -x "$BIN" ]; then
    echo ">> binario ya presente: $BIN"
    exit 0
fi

URL="https://github.com/ggml-org/llama.cpp/releases/download/${TAG}/llama-${TAG}-bin-ubuntu-vulkan-x64.tar.gz"
mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo ">> descargando $URL"
curl -fsSL --retry 3 --max-time 600 -o "$TMP/b.tar.gz" "$URL"

echo ">> extrayendo en $DEST"
tar -xzf "$TMP/b.tar.gz" -C "$DEST"

if [ -x "$BIN" ]; then
    echo ">> listo: $BIN"
else
    echo "FAIL: no aparece $BIN tras extraer" >&2
    exit 1
fi
