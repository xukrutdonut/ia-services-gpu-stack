#!/bin/bash
# Entrypoint para el nodo AMD de LM Studio (Khazad-dum_AMD)
# Inicia llmster daemon y activa LMLink automáticamente
# Requiere: volumen lms_amd_profile ya autenticado con cuenta arkantu

export MESA_VK_DEVICE_SELECT=1002:67df!
export DRI_PRIME=1
export GGML_VK_VISIBLE_DEVICES=0
export PATH=/root/.lmstudio/bin:$PATH

echo "[lms-amd-entrypoint] Iniciando..."

# Esperar a que el volumen esté montado y lms esté disponible
TRIES=0
until [ -f /root/.lmstudio/bin/lms ]; do
    TRIES=$((TRIES+1))
    if [ $TRIES -gt 30 ]; then
        echo "[lms-amd-entrypoint] ERROR: lms no encontrado tras 60s. Abortando."
        exit 1
    fi
    echo "[lms-amd-entrypoint] Esperando a que lms esté disponible... ($TRIES/30)"
    sleep 2
done

# Verificación estricta de GPU AMD Vulkan (Vendor ID 0x1002)
# El contenedor NUNCA debe ejecutar inferencia en CPU.
# IMPORTANTE: vulkaninfo se ejecuta con timeout 10s. Si la GPU está en estado zombie
# (config space 0xff), vulkaninfo se quedaría colgado en D-state forever, bloqueando
# el contenedor y la GPU. El timeout asegura que el proceso se mate antes de eso.
echo "[lms-amd-entrypoint] Verificando presencia de GPU AMD Vulkan (1002:67df)..."
echo "[lms-amd-entrypoint] Verificando presencia de GPU AMD Vulkan (1002:67df)..."
if ! timeout 5 vulkaninfo --summary 2>&1 | grep -iE "vendorID.*0x1002|deviceName.*AMD|Radeon" > /dev/null; then
    echo "[lms-amd-entrypoint] ❌ ERROR CRÍTICO: No se detectó ninguna GPU AMD activa por Vulkan."
    echo "[lms-amd-entrypoint] ❌ Esperando 10s (puede que el driver esté en GPU reset) y abortando."
    sleep 10  # Dar tiempo al driver para terminar el SRESET antes de que el watchdog reaccione
    exit 1
fi
echo "[lms-amd-entrypoint] ✅ GPU AMD Vulkan detectada correctamente."

# Borrar cache de indice de modelos stale.
# LM Studio almacena en .internal/model-index-cache.json el indice de modelos
# descubiertos. Si el bind mount cambia de ruta (ej: de /models a /models/rx480),
# el cache viejo mantiene referencias a rutas inexistentes y los modelos locales
# quedan como "unclassifiedFiles" sin indexar. Borrar el cache fuerza a LM Studio
# a reconstruirlo al iniciar, descubriendo los GGUF en su nueva ubicacion.
if [ -f /root/.lmstudio/.internal/model-index-cache.json ]; then
    echo "[lms-amd-entrypoint] Limpiando model-index-cache.json stale..."
    rm -f /root/.lmstudio/.internal/model-index-cache.json
fi

echo "[lms-amd-entrypoint] lms encontrado. Iniciando daemon llmster..."

# Iniciar llmster daemon en segundo plano
LLMSTER_BIN=$(find /root/.lmstudio/llmster/ -name llmster -type f | head -n 1)
if [ -n "$LLMSTER_BIN" ]; then
    echo "[lms-amd-entrypoint] Ejecutando daemon llmster ($LLMSTER_BIN)..."
    "$LLMSTER_BIN" > /tmp/llmster.log 2>&1 &
else
    echo "[lms-amd-entrypoint] ERROR: no se encontró ejecutable llmster."
    exit 1
fi
sleep 5

# Iniciar servidor HTTP en 0.0.0.0:1234 para soporte API/JIT directo
echo "[lms-amd-entrypoint] Iniciando servidor HTTP API en 0.0.0.0:1234..."
/root/.lmstudio/bin/lms server start --bind 0.0.0.0 --port 1234 || true

# Asegurar instalacion del wrapper de llama-server para estabilidad en AMD Polaris
for backend_dir in /root/.lmstudio/extensions/backends/llama.cpp-linux-x86_64-vulkan-*; do
    if [ -d "$backend_dir" ]; then
        if [ -f "$backend_dir/llama-server" ] && [ ! -f "$backend_dir/llama-server.real" ]; then
            echo "[lms-amd-entrypoint] Respaldando binario original en $backend_dir..."
            mv "$backend_dir/llama-server" "$backend_dir/llama-server.real"
        fi
        if [ -f "$backend_dir/llama-server.real" ]; then
            echo "[lms-amd-entrypoint] Instalando/actualizando wrapper en $backend_dir..."
            cat << 'WRAPPER_EOF' > "$backend_dir/llama-server"
#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_BIN="$DIR/llama-server.real"
if [ ! -f "$REAL_BIN" ]; then
    echo "ERROR: Real llama-server binary not found at $REAL_BIN" >&2
    exit 1
fi

LOCK_FILE="/tmp/llama-server-single-instance.lock"

# Mutex estricto: NUNCA permitir mas de 1 instancia de modelo en la RX 480
(
    flock -x 200
    OLD_PIDS=$(pgrep -f "llama-server.real" | grep -v "^$$$" || true)
    if [ -n "$OLD_PIDS" ]; then
        echo "[$(date -Iseconds)] [WRAPPER] Instancia activa detectada (PIDs: $OLD_PIDS). Terminando para garantizar estrictamente 1 único modelo en RX 480..." >> /tmp/llama-server-wrapper.log
        kill -15 $OLD_PIDS 2>/dev/null || true
        for _ in 1 2 3; do
            REMAINING=$(pgrep -f "llama-server.real" | grep -v "^$$$" || true)
            [ -z "$REMAINING" ] && break
            sleep 1
        done
        REMAINING=$(pgrep -f "llama-server.real" | grep -v "^$$$" || true)
        if [ -n "$REMAINING" ]; then
            echo "[$(date -Iseconds)] [WRAPPER] Forzando kill -9 en PIDs remanentes: $REMAINING" >> /tmp/llama-server-wrapper.log
            kill -9 $REMAINING 2>/dev/null || true
            sleep 1
        fi
        # Pausa crucial para permitir que el driver amdgpu/Vulkan limpie buffers VRAM y fences
        sleep 2
    fi
) 200>"$LOCK_FILE"

export GGML_VK_MAX_NODES_PER_SUBMIT=1
export GGML_VK_DISABLE_ASYNC=1
export GGML_VK_FORCE_MAX_ALLOCATION_SIZE=2147483648
export AMDGPU_TARGETS="gfx803"
export RADV_PERFTEST="sam"
export MESA_VK_DEVICE_SELECT="1002:67df!"
export DRI_PRIME=1

NEW_ARGS=()
SKIP_NEXT=0
for ((i=1; i<=$#; i++)); do
    if [ "$SKIP_NEXT" -eq 1 ]; then
        SKIP_NEXT=0
        continue
    fi
    arg="${!i}"
    next_idx=$((i+1))
    next_arg="${!next_idx}"
    case "$arg" in
        --flash-attn|-fa)
            if [[ "$next_arg" =~ ^(on|off|true|false|0|1)$ ]]; then
                SKIP_NEXT=1
            fi
            ;;
        --flash-attn=*)
            ;;
        --parallel|-np)
            NEW_ARGS+=("$arg" "1")
            SKIP_NEXT=1
            ;;
        --ctx-size|-c)
            # Cap a 8192 (antes 4096). El Qwen3-0.6B Q4_K_M ocupa ~400MB y la RX480
            # tiene 8GB VRAM: sobra para 8192 tokens de KV cache en f16. Open WebUI
            # envia system prompts largos que superan 4096 tokens facilmente.
            if [ -n "$next_arg" ] && [ "$next_arg" -gt 8192 ] 2>/dev/null; then
                NEW_ARGS+=("$arg" "8192")
            elif [ -n "$next_arg" ] && [ "$next_arg" -lt 8192 ] 2>/dev/null; then
                NEW_ARGS+=("$arg" "8192")
            else
                NEW_ARGS+=("$arg" "$next_arg")
            fi
            SKIP_NEXT=1
            ;;
        --batch-size|-b)
            if [ -n "$next_arg" ] && [ "$next_arg" -gt 256 ] 2>/dev/null; then
                NEW_ARGS+=("$arg" "256")
            else
                NEW_ARGS+=("$arg" "$next_arg")
            fi
            SKIP_NEXT=1
            ;;
        --ubatch-size|-ub)
            if [ -n "$next_arg" ] && [ "$next_arg" -gt 64 ] 2>/dev/null; then
                NEW_ARGS+=("$arg" "64")
            else
                NEW_ARGS+=("$arg" "$next_arg")
            fi
            SKIP_NEXT=1
            ;;
        --threads|-t)
            if [ -n "$next_arg" ] && [ "$next_arg" -gt 4 ] 2>/dev/null; then
                NEW_ARGS+=("$arg" "4")
            else
                NEW_ARGS+=("$arg" "$next_arg")
            fi
            SKIP_NEXT=1
            ;;
        *)
            NEW_ARGS+=("$arg")
            ;;
    esac
done
NEW_ARGS+=("--flash-attn" "off")
echo "[$(date -Iseconds)] [WRAPPER] Invoking $REAL_BIN (PID: $$)" >> /tmp/llama-server-wrapper.log
echo "  MODIFIED: ${NEW_ARGS[*]}" >> /tmp/llama-server-wrapper.log
exec "$REAL_BIN" "${NEW_ARGS[@]}"
WRAPPER_EOF
            chmod +x "$backend_dir/llama-server"
        fi
    fi
done

# Esperar a que el servidor este listo y verificar modelos locales
echo "[lms-amd-entrypoint] Verificando modelos locales indexados..."
count_local() {
    /root/.lmstudio/bin/lms ls --json 2>/dev/null | grep -o '"publisher": *"rx480"' | wc -l
}
RETRIES=12
LOCAL_MODELS=0
while [ $RETRIES -gt 0 ]; do
    LOCAL_MODELS=$(count_local)
    if [ "$LOCAL_MODELS" -gt 0 ]; then
        break
    fi
    echo "[lms-amd-entrypoint] Esperando indexacion de modelos locales... ($((13-RETRIES))/12)"
    sleep 5
    RETRIES=$((RETRIES-1))
done
if [ "$LOCAL_MODELS" -gt 0 ]; then
    echo "[lms-amd-entrypoint] ✅ $LOCAL_MODELS modelos locales (publisher=rx480, device=local) indexados correctamente."
else
    echo "[lms-amd-entrypoint] ⚠️  No se detectaron modelos locales indexados tras 60s."
    echo "[lms-amd-entrypoint] ⚠️  Verificar que /root/.lmstudio/models/rx480 tiene estructura rx480/ModelName/file.gguf"
fi

# Habilitar LMLink (autenticado como arkantu en el volumen lms_amd_profile)
# El contenedor tiene su propio namespace IPC, asi que el GUI del host no lo
# detecta como instancia local. LMLink lo ve como un dispositivo remoto.
echo "[lms-amd-entrypoint] Habilitando LMLink como Khazad-dum-AMD-eGPU..."
/root/.lmstudio/bin/lms link set-device-name "Khazad-dum-AMD-eGPU"
/root/.lmstudio/bin/lms link enable
sleep 3

# Verificar estado y logear
STATUS=$(/root/.lmstudio/bin/lms link status 2>&1)
echo "[lms-amd-entrypoint] Estado LMLink:"
echo "$STATUS"

echo "[lms-amd-entrypoint] Nodo AMD listo. Manteniendo proceso activo..."
tail -f /dev/null