#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bench-run.sh -- lanzador del bench seguro de la RX 480.
#
#   ./bench-run.sh --check          # solo diagnostico (host, NO toca la GPU)
#   ./bench-run.sh                  # bench completo (dentro del contenedor)
#   ./bench-run.sh --dry-run        # imprime el plan y sale
#   ./bench-run.sh --canary-only    # solo la prueba de vida
#   ./bench-run.sh --models 0.6b,1.7b
#   ./bench-run.sh --telemetry      # incluye temp/fan/potencia (SMU)
#   ./bench-run.sh --stress         # estres sostenido ~15 min (Causa C)
#   ./bench-run.sh --stress --stress-minutes 30 --stress-gen 2048
#
# El diagnostico previo corre en el host (ve el mismo /sys que el contenedor).
# Si no esta limpio (ring timeouts en el arranque, coredump pendiente, o la
# prueba de vida falla) NO se lanza el bench.
# ---------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONT=ia-gpu-amd-rx480
PY="${HERE}/bench/bench_rx480.py"

if ! command -v python3 >/dev/null 2>&1; then
    echo "FAIL: falta python3 en el host" >&2
    exit 1
fi

echo "================ diagnostico previo (host) ================"
if ! python3 "$PY" --check; then
    echo
    echo ">> El diagnostico NO esta limpio: no lanzo el bench."
    echo ">> Si hubo ring timeout en este arranque, hace falta cold boot."
    exit 1
fi
echo "==========================================================="

if [ "${1:-}" = "--check" ]; then
    exit 0
fi

# 1b) binario de bench (no se versiona; se baja si falta)
if [ ! -x "${HERE}/bench-bin/llama-b11382/llama-bench" ]; then
    echo ">> falta el llama-bench de bench-bin/; lo descargo"
    "${HERE}/bench-fetch-bin.sh" || exit 1
fi

# 'docker compose up -d' aplica la config del compose (volumenes/imagen) y solo
# recrea si hace falta; 'docker start' a secas dejaria el contenedor viejo.
if ! docker inspect -f '{{.State.Running}}' "$CONT" 2>/dev/null | grep -q true; then
    echo ">> $CONT no esta arriba; aplicando config del compose..."
    if ! (cd "${HERE}/.." && timeout 180 docker compose up -d "$CONT"); then
        echo "FAIL: no pude arrancar/recrear el contenedor." >&2
        echo "      Si esta atascado (proceso en D sobre GPU colgada): cold boot." >&2
        exit 1
    fi
    printf ">> esperando a llmster"
    for _ in $(seq 1 30); do
        if docker exec "$CONT" curl -sf --max-time 3 \
             http://localhost:1234/api/v0/models >/dev/null 2>&1; then
            echo " ... listo"
            break
        fi
        printf "."
        sleep 2
    done
    echo
fi

echo "================ bench (contenedor $CONT) ================="
docker exec -i "$CONT" /opt/bench/bench_rx480.py "$@"
rc=$?
echo "==========================================================="
case "$rc" in
    0) echo ">> OK: resultados en ${HERE}/results/ (bench-*.json o stress-*.json)" ;;
    2) echo ">> ABORTADO. Si fue --stress, Causa C reproducida: cold boot obligatorio." ;;
    3) echo ">> GPU WEDGED: no muere el proceso. Cold boot obligatorio." ;;
    4) echo ">> preflight fallo: la GPU no responde a la carga minima. Cold boot." ;;
    5) echo ">> ya hay otro bench en marcha." ;;
    6) echo ">> modelo desconocido." ;;
    *) echo ">> salida inesperada: $rc" ;;
esac
exit "$rc"
