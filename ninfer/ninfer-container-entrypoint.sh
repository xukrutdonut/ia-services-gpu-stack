#!/bin/sh
# Arranca el router como proceso principal del contenedor.
set -eu
INI=/config/ninfer-router.ini
[ -r "$INI" ] || { echo "ninfer: falta el preset $INI" >&2; exit 1; }

# Copiar a RAM los modelos que el preset apunte al tmpfs (swap rapido).
# Gemma:
if grep -Eq '^[[:space:]]*model[[:space:]]*=[[:space:]]*/dev/shm/gemma\.ninfer([[:space:]]|$)' "$INI"; then
  SRC=/models/gemma-4-26B-A4B-it-QAT-Q4_0-MTP-ninfer-v3.ninfer
  DST=/dev/shm/gemma.ninfer
  [ -r "$SRC" ] || { echo "ninfer: falta $SRC" >&2; exit 1; }
  src_size=$(stat -c%s "$SRC")
  dst_size=$(stat -c%s "$DST" 2>/dev/null || printf 0)
  if [ "$src_size" != "$dst_size" ]; then
    echo "ninfer: copiando Gemma a RAM"
    cp "$SRC" "$DST"
  fi
fi

# Qwen3.8-35B-A3B-Distill:
if grep -Eq '^[[:space:]]*model[[:space:]]*=[[:space:]]*/dev/shm/qwen\.ninfer([[:space:]]|$)' "$INI"; then
  SRC=/models/qwen38-35b-a3b-distill.ninfer
  DST=/dev/shm/qwen.ninfer
  [ -r "$SRC" ] || { echo "ninfer: falta $SRC" >&2; exit 1; }
  src_size=$(stat -c%s "$SRC")
  dst_size=$(stat -c%s "$DST" 2>/dev/null || printf 0)
  if [ "$src_size" != "$dst_size" ]; then
    echo "ninfer: copiando Qwen35B a RAM"
    cp "$SRC" "$DST"
  fi
fi

exec /build/apps/ninfer-serve --models-preset "$INI" \
  --host 0.0.0.0 --port 8090 --models-max 1
