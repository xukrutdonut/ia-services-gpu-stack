#!/bin/sh
# ninfer-mgr.sh: carga/descarga de modelos ninfer en la V100 desde Hermes.
#
# Wrapper fino sobre ninfer-serve.sh (aqui vive la logica de flags por familia
# y el manejo del motor); este gestor anade descubrimiento, estado y verificacion.
# NO requiere sudo: todo via docker exec al contenedor ninfer-serve.
#
# Uso:
#   ninfer-mgr.sh list                         # artefactos servibles + cual esta cargado
#   ninfer-mgr.sh status                       # GPU, PID del motor, modelo cargado, salud
#   ninfer-mgr.sh load <modelo|alias> [ctx] [conc] [draft]   # swap a ese artefacto
#   ninfer-mgr.sh unload                       # detiene el motor (libera la VRAM)
#   ninfer-mgr.sh restart                      # relanza el modelo actual
#   ninfer-mgr.sh ps                           # compute apps en la V100
set -u

MODELS_DIR="$HOME/produccion/ninfer-v100/models"
STACK_DIR="$HOME/produccion/ia-services-stack"
SERVE="$STACK_DIR/ninfer-serve.sh"
STATE="$STACK_DIR/.ninfer-current"
NGTABLE='Qwen3.8-Flash-Next-ngram-table-IQ4_NL-ninfer-v3.ninfer'

# Alias -> fichero. Los alias son los que valen para servir directo (el
# n-gram-table no es un modelo y se lista aparte).
resolve_alias() {
  case "$1" in
    gemma|gemma4|gemma-4) echo 'gemma-4-26B-A4B-it-QAT-Q4_0-MTP-ninfer-v3.ninfer';;
    qwen35|qwen35-nvfp4|nvfp4) echo 'Qwen3.6-35B-A3B-NVFP4-ninfer-v3.ninfer';;
    qwen35-v3|qwen-v3|v3) echo 'qwen3_6_35b_a3b-v3.ninfer';;
    *) echo "$1";;
  esac
}

# Relee el modelo cargado: primero el estado registrado (escrito en load),
# si no cuadra, lo infiere del tamano que el motor reporta en /v1/models
# (campo "size", exacto; el fichero en disco puede diferir ~20-30 MB porque el
# engine deduplica secciones, asi que se compara con tolerancia de 50 MB).
current_model() {
  if [ -f "$STATE" ]; then
    local m; m="$(cat "$STATE")"
    [ -f "$MODELS_DIR/$m" ] && { echo "$m"; return; }
  fi
  local esz; esz="$(curl -sS --max-time 5 http://127.0.0.1:8090/v1/models 2>/dev/null \
    | sed -n 's/.*"size":\([0-9]*\).*/\1/p' | head -1)"
  [ -z "$esz" ] && { echo "(motor cargado, artefacto indeterminado)"; return; }
  for f in "$MODELS_DIR"/*.ninfer; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in "$NGTABLE") continue;; esac
    local fs d; fs="$(stat -c%s "$f")"; d=$(( fs - esz )); [ "$d" -lt 0 ] && d=$((-d))
    if [ "$d" -lt 52428800 ]; then echo "$(basename "$f")"; return; fi
  done
  echo "(motor cargado, artefacto sin coincidencia)"
}

list_models() {
  echo "Artefactos en $MODELS_DIR:"
  local cur; cur="$(current_model)"
  local n=0
  for f in "$MODELS_DIR"/*.ninfer; do
    [ -f "$f" ] || continue
    local base; base="$(basename "$f")"
    local tag=""
    case "$base" in
      "$NGTABLE") tag=" [n-gram-table, NO es modelo]";;
      *) ;;
    esac
    local sz; sz="$(awk -v s="$(stat -c%s "$f")" 'BEGIN{printf "%.1f GiB", s/1073741824}')"
    local mark="  "; [ "$base" = "$cur" ] && mark="=>"
    printf '%s [%s] %-64s %s%s\n' "$mark" "$n" "$base" "$sz" "$tag"
    n=$((n+1))
  done
  # tmp en curso (conversion/descarga)
  for f in "$MODELS_DIR"/.tmp "$MODELS_DIR"/*.tmp; do
    [ -f "$f" ] && { local b; b="$(basename "$f")"; printf '   (en curso) %s\n' "$b"; }
  done
  echo
  echo "Cargado ahora: $cur"
}

status() {
  echo "== V100 =="
  nvidia-smi --query-gpu=name,memory.used,memory.total,temperature.gpu --format=csv,noheader 2>&1
  echo "== Compute apps =="
  nvidia-smi --query-compute-apps=pid,name,used_memory --format=csv,noheader 2>&1
  echo "== Modelo cargado =="
  echo "  $(current_model)"
  echo "== Salud :8090 =="
  local models; models="$(curl -sS --max-time 5 http://127.0.0.1:8090/v1/models 2>&1)"
  local rc=$?
  if [ $rc -eq 0 ] && [ -n "$models" ]; then
    echo "  /v1/models OK: $models"
  else
    echo "  /v1/models FALLA (rc=$rc): $models"
  fi
}

do_load() {
  local target; target="$(resolve_alias "$1")"
  if [ ! -f "$MODELS_DIR/$target" ]; then
    echo "ERROR: no existe $MODELS_DIR/$target"
    echo "Usa 'ninfer-mgr.sh list' para ver los disponibles."
    exit 1
  fi
  if [ "$target" = "$NGTABLE" ]; then
    echo "ERROR: $target es una tabla n-gram, no un modelo de pesos."
    echo "El motor la rechaza como modelo (necesita --ngram-table junto a un modelo real)."
    exit 1
  fi
  echo "Ninfer: cargando $target (ctx=${2:-65536} conc=${3:-1} draft=${4:-})..."
  if ! "$SERVE" "$target" "${2:-65536}" "${3:-1}" "${4:-}"; then
    echo "ERROR: ninfer-serve.sh no completó. Mira /tmp/serve.log."
    exit 1
  fi
  echo "$target" > "$STATE"
  # verificacion: esperar a que :8090 responda y el motor tenga VRAM residente
  local ok=0
  for i in $(seq 1 30); do
    curl -sS --max-time 5 http://127.0.0.1:8090/v1/models >/dev/null 2>&1 && ok=1 && break
    sleep 2
  done
  if [ "$ok" = 1 ]; then
    echo "OK: $target sirviendo en :8090 (load ~$(awk -v s="$(stat -c%s "$MODELS_DIR/$target")" 'BEGIN{printf "%.1f", s/1073741824}') GiB)"
  else
    echo "AVISO: la carga no respondió en :8090 en 60s. Revisa /tmp/serve.log."
  fi
}

do_unload() {
  echo "Ninfer: descargando el motor (libera la VRAM)..."
  "$SERVE" stop
  rm -f "$STATE"
  # verificar: el compute app grande (>1 GiB) debe desaparecer
  local left; left="$(nvidia-smi --query-compute-apps=used_memory --format=csv,noheader 2>/dev/null | awk -F' ' '{if ($1+0>1024) print}')"
  if [ -z "$left" ]; then
    echo "OK: VRAM liberada, sin motor ninfer residente."
  else
    echo "AVISO: quedan apps en GPU (>1 GiB): $left"
    echo "      (puede ser otro motor: strata, llama-swap u openvino-embedding)"
  fi
}

do_restart() {
  local m; m="$(current_model)"
  case "$m" in
    "("* ) echo "ERROR: no puedo relanzar, modelo actual desconocido: $m"; exit 1;;
  esac
  echo "Ninfer: relanzando $m"
  "$SERVE" "$m" "${2:-65536}" "${3:-1}" "${4:-}"
  echo "$m" > "$STATE"
}

run_ps() {
  nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>&1
}

case "${1:-}" in
  list)    list_models;;
  status)  status;;
  ps)      run_ps;;
  load)    [ $# -lt 2 ] && { echo "Uso: ninfer-mgr.sh load <modelo|alias> [ctx] [conc] [draft]"; exit 1; }
           do_load "$2" "${3:-}" "${4:-}" "${5:-}";;
  unload|stop) do_unload;;
  restart) do_restart "$@";;
  *)       echo "Uso: ninfer-mgr.sh {list|status|ps|load <modelo> [ctx] [conc] [draft]|unload|restart}"
           echo "Alias: gemma | qwen35(=nvfp4) | qwen35-v3 | (o un nombre de fichero en '$MODELS_DIR')";;
esac
