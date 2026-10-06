#!/bin/bash
# Entrypoint para el nodo AMD de LM Studio (Khazad-dum_AMD)
# Inicia llmster daemon y activa LMLink automáticamente
# Requiere: volumen lms_amd_profile ya autenticado con cuenta arkantu

export MESA_VK_DEVICE_SELECT=1002:67df!
export DRI_PRIME=1
export GGML_VK_VISIBLE_DEVICES=0
export PATH=/root/.lmstudio/bin:$PATH

# CORREGIDO 2026-10-01: el token real de Mesa/RADV es 'nosam' (sin guion), no
# 'no_sam' — con guion se ignoraba silenciosamente. Verificado con:
#   strings libvulkan_radeon.so | grep -x nosam   -> presente
#   strings libvulkan_radeon.so | grep -x no_sam  -> AUSENTE
# Y ADEMAS, en Mesa 25.2.8 'nosam' ya no es un RADV_PERFTEST sino una driconf
# option, asi que hoy seria un no-op igualmente. Se deja documentado como
# referencia historica; la linea real esta eliminada/desactivada porque el valor
# correcto no aportaba nada (vulkaninfo seguia reportando sparseResidency=true).
# IMPORTANTE: no usar `vulkaninfo` para "comprobar" la GPU: crea un device
# Vulkan con sparse residency (set_prt=1) y deja amdgpu con la VM faults
# desactivadas para TODO el arranque ("Disabling VM faults because of PRT
# request!"), quitando la red de seguridad ante page faults de la GPU.

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

# Verificación estricta de GPU AMD disponible.
# IMPORTANTE: NO usar vulkaninfo --summary porque probea sparse resources y
# puede disparar PRT incluso con RADV_PERFTEST=no_sam. En su lugar, verificar
# via sysfs que el dispositivo DRM existe y tiene VRAM.
echo "[lms-amd-entrypoint] Verificando GPU AMD via sysfs..."
# NO hardcodear card0/card1 ni renderD128/renderD129: el kernel reenumera las
# GPUs entre arranques (visto tras actualizar a 7.0.0-38: la RX480 pasó de
# card1/renderD128 a card0/renderD129 y la Arc al revés). Se localiza la GPU
# AMD por PCI vendor (0x1002) recorriendo /sys/class/drm.
AMD_CARD=""
for c in /sys/class/drm/card[0-9]*; do
    [ -e "$c/device/vendor" ] || continue
    v=$(cat "$c/device/vendor" 2>/dev/null)
    if [ "$v" = "0x1002" ]; then
        AMD_CARD=$(basename "$c")
        break
    fi
done
if [ -z "$AMD_CARD" ] || [ ! -e "/dev/dri/$AMD_CARD" ]; then
    echo "[lms-amd-entrypoint] ❌ ERROR CRÍTICO: no hay GPU con vendor 0x1002 visible en /dev/dri."
    echo "[lms-amd-entrypoint] ❌ La GPU AMD no está disponible. Abortando."
    sleep 10
    exit 1
fi
echo "[lms-amd-entrypoint] ✅ GPU AMD detectada via sysfs: $AMD_CARD (vendor=0x1002)."

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

# ==========================================================================
# PERSISTENCIA DEL CONTEXTO (corregido 2026-10-02)
# --------------------------------------------------------------------------
# SINTOMA: OpenWebUI devolvia
#   400 Engine protocol predict request returned 400:
#   "request (8744 tokens) exceeds the available context size (8192 tokens)"
# CAUSA: http-server-config.json tiene justInTimeModelLoading=true, asi que LM
# Studio AUTO-CARGA el modelo por JIT cuando llega una peticion y el modelo no
# esta cargado. Ese camino JIT NO usa el flag `-c 32768` del `lms load` de mas
# abajo: usa `defaultContextLength` de /root/.lmstudio/settings.json, que venia
# en {type:custom, value:4096}. El wrapper de llama-server normaliza a un minimo
# de 8192 (ver bloque --ctx-size|-c mas abajo), de modo que:
#   4096 (settings.json) -> normalizado 8192 -> --ctx-size 8192
# Cualquier prompt > 8192 tokens fallaba. El `-c 32768` solo se respetaba en el
# arranque limpio; la primera recarga JIT lo degradaba a 8192 y el cambio se
# perdia ("regresionaba con el reinicio").
# FIX: forzar defaultContextLength=32768 en cada arranque, de forma idempotente
# y sin depender del estado del volumen. llmster lee settings.json en vivo, por
# lo que el valor debe estar escrito ANTES de arrancar el daemon (es este punto).
# ==========================================================================
SETTINGS_JSON=/root/.lmstudio/settings.json
NODE_BIN=/root/.lmstudio/.internal/utils/node
export SETTINGS_JSON
echo "[lms-amd-entrypoint] Fijando defaultContextLength=32768 en settings.json..."
if [ -f "$SETTINGS_JSON" ] && [ -x "$NODE_BIN" ]; then
    "$NODE_BIN" -e '
        const fs = require("fs");
        const p = process.env.SETTINGS_JSON;
        const j = JSON.parse(fs.readFileSync(p, "utf8"));
        const cur = j.defaultContextLength && j.defaultContextLength.value;
        if (cur !== 32768) {
            j.defaultContextLength = { type: "custom", value: 32768 };
            fs.writeFileSync(p, JSON.stringify(j, null, 2));
            console.log("  defaultContextLength: " + cur + " -> 32768 (actualizado)");
        } else {
            console.log("  defaultContextLength ya era 32768 (sin cambios)");
        }
    '
else
    echo "  ⚠️  AVISO: no se pudo fijar el contexto (falta $SETTINGS_JSON o $NODE_BIN)."
    echo "  ⚠️  El JIT puede volver a cargar el modelo con ctx 8192 y OpenWebUI fallara."
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

# NOTA: llmster ya arranca su propio servidor HTTP en 0.0.0.0:1234.
# No ejecutar `lms server start` aqui — causa EADDRINUSE -> PID lock loss -> API cae.

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
export MESA_VK_DEVICE_SELECT="1002:67df!"
export DRI_PRIME=1
# RADV_PERFTEST y GGML_VK_ALLOW_EXOTIC NO se exportan aqui (se retiraron por
# inestabilidad en Polaris; ver DIAGNOSTICO-RX480-2026-10-01.md). Lo que si se
# exporta en esta linea son MESA_VK_DEVICE_SELECT / DRI_PRIME / gfx803.

NEW_ARGS=()
SKIP_NEXT=0
MODEL_PATH=""
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
            # Cap 32768 (RX480 8 GiB = 8,59 GB decimales). Suelo por modelo:
            #  - Embeddings: sin suelo (ctx nativo corto, nadie les manda prompts
            #    largos; forzarles 16k solo gastaria VRAM).
            #  - OLMoE: 16384 (justificado abajo).
            #  - Resto: 16384.
            # Motivo del suelo: LM Studio pide 4096 a OLMoE (max_context_length
            # del GGUF) y con 4096 OpenWebUI/OpenInterpreter rechazan cualquier
            # prompt >4096. OLMoE tiene n_ctx_train=4096: por encima la calidad
            # degrada (RoPE extrapolado) pero la peticion NO se rechaza.
            # Trade-off aceptado y documentado en PENDIENTE-BENCH-RX480.md §8.
            # VRAM MEDIDA (2026-10-04, KV de OLMoE = 128 KB/token: 16 capas x 16
            # cabezas KV x 128 dim x 2 (K+V) x 2 B; pesos Q4_K_M ~4,0 GB):
            #   8192  f16    -> 5,02 GB usados (3,6 GB libres)
            #   16384 f16    -> 6,40 GB usados (2,2 GB libres)  <- ELEGIDO
            #   32768 f16    -> NO ARRANCA: KV 4,0 GB + pesos 4,0 GB + buffers >
            #                   8 GiB -> "Engine protocol startup was aborted"
            #   32768 K/q8_0 -> 7,75 GB usados (0,45 GB libres)
            #
            # Por que 16384 y no 32768 para OLMoE (decision 2026-10-04): OLMoE tiene
            # n_ctx_train=4096, asi que mas contexto NO mejora la calidad, solo evita
            # que la API rechace el prompt. A 32768 el margen es ~0,45 GB y este
            # contenedor tiene un fallo conocido de oversubscription (el wrapper puede
            # lanzar 2-3 llama-server.real en paralelo), que con la VRAM al limite
            # produce "Engine protocol startup was aborted" y teardown sucio del kernel
            # ("leaking bo va") -> riesgo de wedge con cold boot fisico.
            FLOOR=16384
            case "$MODEL_PATH" in
                # Embeddings: sin suelo (ctx nativo corto, nadie les manda prompts largos).
                *mbed*) FLOOR=0;;
                # OLMoE: 16384 (ver justificacion arriba).
                *[Oo][Ll][Mm][Oo][Ee]*) FLOOR=16384;;
            esac
            if [ -n "$next_arg" ] && [ "$next_arg" -gt 32768 ] 2>/dev/null; then
                NEW_ARGS+=("$arg" "32768")
            elif [ "$FLOOR" -gt 0 ] && [ -n "$next_arg" ] && [ "$next_arg" -lt "$FLOOR" ] 2>/dev/null; then
                NEW_ARGS+=("$arg" "$FLOOR")
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
        --model|-m)
            # Guardamos la ruta para que el suelo de --ctx-size pueda ser
            # selectivo (los embeddings no lo llevan). El valor va detras y cae
            # en el caso *) de la siguiente vuelta, como antes.
            MODEL_PATH="$next_arg"
            NEW_ARGS+=("$arg")
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

echo "[lms-amd-entrypoint] Nodo AMD listo."

# Cargar modelo qwen3-1.7b-instruct persistentemente (full GPU offload)
echo "[lms-amd-entrypoint] Cargando modelo qwen3-1.7b-instruct (--gpu max)..."
/root/.lmstudio/bin/lms load qwen3-1.7b-instruct --gpu max -c 32768 -y 2>&1 || \
    echo "[lms-amd-entrypoint] WARNING: No se pudo cargar qwen3-1.7b-instruct automáticamente."

# Arrancar el API server HTTP en 0.0.0.0:1234.
# Tras el PID lock loss causado por lms link enable, llmster reviva con lms load
# pero sin el server HTTP. Hay que arrancarlo explicitamente con --bind 0.0.0.0
# para que sea accesible desde fuera del contenedor (puerto mapeado 1235->1234).
echo "[lms-amd-entrypoint] Arrancando API server en 0.0.0.0:1234..."
RETRIES=5
while [ $RETRIES -gt 0 ]; do
    if /root/.lmstudio/bin/lms server start --bind 0.0.0.0 --port 1234 2>&1; then
        echo "[lms-amd-entrypoint] ✅ API server arrancado en 0.0.0.0:1234."
        break
    fi
    echo "[lms-amd-entrypoint] Reintentando server start... ($((6-RETRIES))/5)"
    sleep 3
    RETRIES=$((RETRIES-1))
done
if [ $RETRIES -eq 0 ]; then
    echo "[lms-amd-entrypoint] ⚠️ No se pudo arrancar el API server tras 5 intentos."
fi

echo "[lms-amd-entrypoint] Manteniendo proceso activo..."
tail -f /dev/null