#!/bin/sh
# ovms-mgr.sh: carga/descarga de modelos OpenVINO (OVMS :8006, iGPU Arc) desde Hermes.
#
# OVMS sirve VARIOS modelos a la vez por config (a diferencia de ninfer, 1 slot).
# Reglas de la casa (ver ovms-model): los embeddings SIEMPRE estan activos; los
# LLM grandes se descargan antes de cargar otro porque dos grandes a la vez agotan
# el GTT compartido -> CL_OUT_OF_RESOURCES -> wedge del driver xe (freeze).
# Id de modelo CON MAYUSCULAS y exacto (OVMS devuelve 404 si no cuadra): el wrapper
# resuelve aliases/prefix a un id exacto presente en disco.
#
# Este gestor delimita contra los directorios reales en /models: si un id solo esta
# en config.all-models.json (master) pero no hay dir en disco, load lo rechaza a
# proposito (OVMS pondria el modelo en END y el reload fallaria).
#
# Uso:
#   ovms-mgr.sh list        # modelos en disco + cual esta servido
#   ovms-mgr.sh status      # /v1/models (ids servidos) + estados /v1/config
#   ovms-mgr.sh load <modelo|alias>   # embeddings + solo ese LLM (delega en ovms-model only)
#   ovms-mgr.sh on          # Qwen3 + LFM2 (AVISO: riesgo de GPU HANG si ambos estan en disco)
#   ovms-mgr.sh off         # vuelve al default: Qwen3 + embeddings (el "unload" de OVMS)
#   ovms-mgr.sh reload      # reaplica el config actual
set -u

IR=/home/arkantu/modelos/.openvino-ir
MASTER=$IR/config.all-models.json
OVM=~/.local/bin/ovms-model
EMBS='nomic|embed'      # prompts: los embeddings nuncan se descargan

# Los LLM grandes listados en el master (para mostrarlos aun sin dir).
master_llms() {
  [ -f "$MASTER" ] || return 0
  python3 -c "import json,sys;c=json.load(open('$MASTER'))['model_config_list'];
import re
print('\n'.join(x['config']['name'] for x in c if not re.search('embed|nomic',x['config']['name'],re.I)))"
}

# Resuelve alias/prefix de usuario a un id EXACTO entre los dirs de LLM en disco.
# Devuelve vacio si no hay coincidencia unica. Embeds excluidos (siempre on).
resolve() {
  local want="$1" hits
  hits=$(for d in "$IR"/*/; do
    b=$(basename "$d")
    case "$b" in nomic*|qwen3-embed*) continue;; esac
    case "$b" in
      "$want"|"$want"*|*"$want"*) echo "$b";;
    esac
  done)
  case "$(printf '%s\n' "$hits" | grep -c .)" in
    1) echo "$hits";;
    0) echo "";;
    *) echo "AMBIGUO:$hits";;
  esac
}

loadable_list() {
  local cur; cur="$({ "$OVM" status 2>/dev/null || true; } | sed 's/^ *//')"
  echo "Modelos OpenVINO (iGPU Arc, :8006):"
  echo "  [LLM]  — solo uno grande a la vez (los embeddings van siempre activos):"
  local n=0
  for d in "$IR"/*/; do
    [ -d "$d" ] || continue
    local b; b="$(basename "$d")"
    case "$b" in
      nomic*|qwen3-embed*) continue;;
    esac
    local mark="  "; echo "$cur" | grep -qF "$b" && mark="=>"
    printf '  %s   %s\n' "$mark" "$b"
    n=$((n+1))
  done
  [ "$n" -eq 0 ] && echo "  (ningun LLM en disco)"
  echo
  echo "  [NO en disco — en master, pero load los rechazara]:"
  for m in $(master_llms); do
    [ -d "$IR/$m" ] && continue
    echo "    - $m"
  done
  echo
  echo "  [Embeddings (siempre activos)]:"
  for d in "$IR"/*/; do
    case "$(basename "$d")" in nomic*|qwen3-embed*) echo "    * $(basename "$d")";; esac
  done
}

do_status() {
  echo "== OVMS :8006 =="
  if ! curl -sf --max-time 5 http://127.0.0.1:8006/v1/models >/dev/null 2>&1; then
    echo "  /v1/models NO responde. Contenedor ia-gpu-intel-openvino parado?"; return 1
  fi
  { "$OVM" status; } 2>&1 | sed 's/^/  /'
  echo "  estados /v1/config:"
  curl -sf --max-time 20 http://127.0.0.1:8006/v1/config | python3 -c '
import json,sys
for name,v in json.load(sys.stdin).items():
    st=v["model_version_status"][-1]["state"]
    print(f"    {name:42} {st}")'
}

# Un solo LLM en disco actualmente => on no tiene sentido salvo que exista LFM2.
ready_for_on() {
  local have; have=$(master_llms | grep -ciF 'LFM2-24B')
  [ "$have" -eq 1 ] && [ -d "$IR/LFM2-24B-A2B-int4-ov" ]
}

case "${1:-}" in
  list)   loadable_list;;
  status) do_status;;
  load)
    [ $# -lt 2 ] && { echo "Uso: ovms-mgr.sh load <modelo|alias>"; exit 1; }
    id=$(resolve "$2")
    case "$id" in
      AMBIGUO:*) echo "ERROR: el prefijo es ambiguo: $id"; echo "  Usa el id completo: `master_llms`"; exit 1;;
      "") echo "ERROR: no hay un LLM '${2}' en disco (${IR}). Lista:"
          (cd "$IR" && ls -d */)
          echo "Si el modelo esta solo en el master pero sin dir, no se puede cargar aun."; exit 1;;
    esac
    echo "OpenVINO: cargando $id (embeddings + solo este LLM)..."
    "$OVM" only "$id"
    ;;
  on)
    if ! ready_for_on; then
      echo "ERROR: 'on' necesita LFM2-24B-A2B-int4-ov (no esta en disco ahora mismo)."
      echo "  Con solo Qwen3 en disco, el equivalente es: ovms-mgr.sh load Qwen3"
      exit 1
    fi
    echo "OpenVINO: Qwen3 + LFM2 (AVISO: riesgo de GPU HANG por GTT compartido)."
    "$OVM" on
    ;;
  off|unload)
    echo "OpenVINO: volviendo al default (Qwen3 + embeddings)."
    shift
    "$OVM" off
    ;;
  reload) "$OVM" reload;;
  *) echo "Uso: ovms-mgr.sh {list|status|load <modelo|alias>|on|off|reload}"
     echo "  Alias actual: qwen3|qwen30 => Qwen3-30B-A3B-Instruct-2507-int4-ov"
     echo "  (unload = off: en OVMS los embeddings no se descargan, y el LLM vuelve a Qwen3)";;
esac
