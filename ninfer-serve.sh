#!/bin/sh
# Relanza el motor ninfer de la V100 (contenedor ninfer-serve, CMD = sleep infinity).
# Uso: ./ninfer-serve.sh [modelo.ninfer] [max-context] [concurrencia] [kv-dtype] [draft-tokens]
#   kv-dtype: bf16 (defecto) | int8 | fp8 | nvfp4 | k8v4   (nvfp4/k8v4 no existen en Volta)
# --model-id se fija a "ninfer": el id de los clientes directos no cambia al cambiar de artefacto.
# --vision es OBLIGATORIO para imagenes/video: el artefacto lleva la torre de vision,
# pero el runtime solo la carga con ese flag (~330 MiB de VRAM).
set -eu
MODEL="${1:-qwen3_6_35b_a3b_v2.ninfer}"
CTX="${2:-262144}"
CONC="${3:-2}"
KV="${4:-bf16}"
DRAFT="${5:-2}"
# El motor ignora SIGTERM: TERM, espera 10 s, y luego SIGKILL (si no, se queda con la VRAM
# y el arranque nuevo muere con "only N bytes are free before loading weights").
docker exec ninfer-serve sh -c 'for p in $(pgrep -f "^/src/build-v100/apps/ninfer-serve"); do kill "$p" 2>/dev/null; done
  for i in $(seq 1 10); do pgrep -f "^/src/build-v100/apps/ninfer-serve" >/dev/null || break; sleep 1; done
  for p in $(pgrep -f "^/src/build-v100/apps/ninfer-serve"); do kill -9 "$p" 2>/dev/null; done; true'
for i in $(seq 1 30); do
  nvidia-smi --query-compute-apps=pid --format=csv,noheader | grep -q . || break
  sleep 1
done
docker exec -d ninfer-serve sh -c "setsid /src/build-v100/apps/ninfer-serve /models/$MODEL \
  --model-id ninfer \
  --host 0.0.0.0 --port 8090 \
  --max-context $CTX --max-concurrency $CONC --default-max-tokens 8192 \
  --kv-dtype $KV --prefill-chunk 2048 \
  --spec mtp --draft-tokens $DRAFT \
  --vision --media-cache-mib 1024 --media-live-mib 2048 \
  > /tmp/serve.log 2>&1 < /dev/null"
echo "ninfer: $MODEL ctx=$CTX conc=$CONC kv=$KV draft=$DRAFT (vision ON) id=ninfer -> :8090"
