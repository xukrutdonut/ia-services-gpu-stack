#!/bin/sh
# Relanza el motor ninfer en la V100 (runtime = ninfer-v100:bench, contenedor ninfer-serve).
# El binario vive DENTRO de la imagen (/build/apps/ninfer-serve); solo se montan los modelos.
# Uso: ./ninfer-serve.sh [modelo.ninfer] [max-context] [concurrencia] [draft-tokens]
#   default = gemma-4-26B-A4B-it-QAT-Q4_0-MTP-ninfer-v3.ninfer: produccion 2026-11-11.
#     Medido en la V100 (ctx 32k, 1 slot, draft 2, enable_thinking=false): decode 207 tok/s
#     sostenido, prefill 1437 tok/s a 1,1k de prompt, ~20 GiB de VRAM. Tool calling nativo
#     y multimodal. Sustituye al 35B porque decodifica 1,7x mas rapido y deja 11 GiB libres:
#     con el 35B la tarjeta iba a 31,4/32 GiB y 79 C, y en ese estado se cayo del bus (Xid 79).
#   Respaldo con mas conocimiento: qwen3_6_35b_a3b-v3.ninfer (22 GiB de pesos, decode ~120,
#     prefill 799, VRAM 31,4 GiB). Es el v2 del repo convertido con tools/upgrade_ninfer_v2_to_v3.py.
# Los defaults son el preset de Hermes (ctx 65536, 1 slot, draft 2):
#   Hermes rechaza modelos con ventana < 64k (agent_init._enforce_minimum_context), asi que el
#   motor tiene que anunciar al menos 65536 aunque el cliente mande prompts de ~8k.
#   el KV de 256k no aporta a un cliente que manda prompts de ~8k y arrastra prefill;
#   con varios clientes: ./ninfer-serve.sh <modelo> 65536 3 3
# --model-id se fija a "ninfer" (NINFER_MODEL_ID lo cambia): el id de los clientes no cambia
# al cambiar de artefacto.
# --vision carga la torre de vision del artefacto (~330 MiB de VRAM); ambos la llevan.
set -eu
# Modo stop: mismo TERM/KILL anclado que usa el arranque (lo llama ExecStop de la unit).
if [ "${1:-}" = stop ]; then
  docker exec ninfer-serve sh -c "for p in \$(pgrep -f '^/build/apps/ninfer-serve'); do kill \"\$p\" 2>/dev/null; done
    for i in \$(seq 1 10); do pgrep -f '^/build/apps/ninfer-serve' >/dev/null || break; sleep 1; done
    for p in \$(pgrep -f '^/build/apps/ninfer-serve'); do kill -9 \"\$p\" 2>/dev/null; done; true" || true
  echo "ninfer: motor detenido"
  exit 0
fi
MODEL="${1:-gemma-4-26B-A4B-it-QAT-Q4_0-MTP-ninfer-v3.ninfer}"
CTX="${2:-65536}"
CONC="${3:-1}"
DRAFT="${4:-}"
MODEL_ID="${NINFER_MODEL_ID:-ninfer}"
# BIN_RE=patron del proceso dentro del contenedor para TERM/KILL (el motor ignora SIGTERM).
# OJO: debe ir ANCLADO al binario real. Un patron suelto ("ninfer-serve") tambien coincide con
# la cmdline del propio `sh -c` que lanza el docker exec -> el script se mata solo (status=143).
BIN="/build/apps/ninfer-serve"
BIN_RE='^/build/apps/ninfer-serve'

# Flags por familia de artefacto. Los densos/MoE GGUF (Gemma 4) no llevan los extras de Qwen:
# --mtp-experts-q4 y --gdn-state-fp16 son de Qwen3.6-35B-A3B y el motor los rechaza fuera de el.
case "$MODEL" in
  *[Gg]emma*)
    FAMILY=gemma4
    DRAFT="${DRAFT:-2}"
    KV=bf16
    EXTRA=""
    ;;
  *35B-A3B*|*35b_a3b*)
    FAMILY=qwen36
    DRAFT="${DRAFT:-3}"
    KV=rk4v4
    EXTRA="--lm-head-draft --mtp-experts-q4 --gdn-state-fp16"
    ;;
  *)
    FAMILY=other
    DRAFT="${DRAFT:-2}"
    KV=rk4v4
    EXTRA="--lm-head-draft"
    ;;
esac

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
  --model-id $MODEL_ID \
  --host 0.0.0.0 --port 8090 \
  --max-context $CTX --max-concurrency $CONC --default-max-tokens 8192 \
  --kv-dtype $KV --prefill-cublas --prefill-chunk 2048 \
  --spec mtp --draft-tokens $DRAFT $EXTRA \
  --vision --media-cache-mib 1024 --media-live-mib 2048 \
  > /tmp/serve.log 2>&1 < /dev/null"
echo "ninfer: $MODEL ($FAMILY) ctx=$CTX conc=$CONC draft=$DRAFT kv=$KV (vision ON) id=$MODEL_ID -> :8090"
