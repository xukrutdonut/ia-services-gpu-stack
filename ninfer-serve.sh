#!/bin/sh
# Relanza el motor ninfer en la V100 (runtime = ninfer-v100:bench, contenedor ninfer-serve).
# El binario vive DENTRO de la imagen (/build/apps/ninfer-serve); solo se montan los modelos.
# Uso: ./ninfer-serve.sh [modelo.ninfer] [max-context] [concurrencia] [draft-tokens]
#   default = Qwen3.8-Flash-Next IQ4_NL (ngram-table), el "mas rapido y mejor calidad" (2026-11-11).
#   pasar el nombre del artefacto (solo el fichero, /models/ se antepone) para servir otro.
# --model-id se fija a "ninfer": el id de los clientes directos no cambia al cambiar de artefacto.
# --vision es OBLIGATORIO para imagenes/video: el artefacto lleva la torre de vision,
# pero el runtime solo la carga con ese flag (~330 MiB de VRAM).
set -eu
MODEL="${1:-Qwen3.8-Flash-Next-ngram-table-IQ4_NL-ninfer-v3.ninfer}"
CTX="${2:-262144}"
CONC="${3:-2}"
DRAFT="${4:-3}"
# BIN_RE=patron del proceso dentro del contenedor para TERM/KILL (el motor ignora SIGTERM).
BIN="/build/apps/ninfer-serve"
BIN_RE='ninfer-serve'
# El motor ignora SIGTERM: TERM, espera 10 s, y luego SIGKILL (si no, se queda con la VRAM
# y el arranque nuevo muere con "only N bytes are free before loading weights").
docker exec ninfer-serve sh -c "for p in \$(pgrep -f '$BIN_RE'); do kill \"\$p\" 2>/dev/null; done
  for i in \$(seq 1 10); do pgrep -f '$BIN_RE' >/dev/null || break; sleep 1; done
  for p in \$(pgrep -f '$BIN_RE'); do kill -9 \"\$p\" 2>/dev/null; done; true"
for i in $(seq 1 30); do
  nvidia-smi --query-compute-apps=pid --format=csv,noheader | grep -q . || break
  sleep 1
done
docker exec -d ninfer-serve sh -c "setsid $BIN /models/$MODEL \
  --model-id ninfer \
  --host 0.0.0.0 --port 8090 \
  --max-context $CTX --max-concurrency $CONC --default-max-tokens 8192 \
  --kv-dtype rk4v4 --prefill-cublas --prefill-chunk 2048 \
  --spec mtp --draft-tokens $DRAFT --lm-head-draft --lm-head-q6 \
  --embedding-q4 --gdn-state-fp16 \
  --vision --media-cache-mib 1024 --media-live-mib 2048 \
  > /tmp/serve.log 2>&1 < /dev/null"
echo "ninfer: $MODEL ctx=$CTX conc=$CONC draft=$DRAFT (vision ON) id=ninfer -> :8090"
